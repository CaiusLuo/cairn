import os
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from time import perf_counter

from rich.console import Console, ConsoleOptions, Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from cairn.core.events import Event
from cairn.core.model_manager import ModelConfig
from cairn.core.permissions import PermissionChoice, PermissionRequest
from cairn.core.provider_catalog import NamedProvider
from cairn.terminal.live import LiveRegion
from cairn.terminal.thinking import ThinkingPreview

console = Console()

# A tool argument long enough to dominate its status line is summarised by the
# tool name alone; the full value is in the trace.
MAX_ARGUMENT_CHARS = 120
# Failure output beyond this many lines moves to the trace instead of the screen.
MAX_DIAGNOSTIC_LINES = 20
# The most trailing reasoning lines that stay on screen. The model can reason
# for thousands of tokens; a reader wants the current thread, not the transcript.
MAX_THINKING_LINES = 6
# The fewest, so the thread never loses its shape on a short screen.
MIN_THINKING_LINES = 2
# A quarter of the screen is reasoning at most; the answer and the status line
# need the rest. Short screens therefore show fewer lines than MAX_THINKING.
THINKING_SCREEN_SHARE = 4
# Bar segments in the context meter.
_BAR_WIDTH = 8
# Status updates are painted at this rate at most. A token stream can deliver
# dozens of deltas a second; repainting per delta costs far more than a reader
# can perceive.
STATUS_INTERVAL_SECONDS = 0.1


@dataclass(frozen=True)
class Glyphs:
    """The characters this terminal can render.

    A terminal without a Unicode font shows box-drawing and geometric shapes as
    replacement characters, so every symbol has an ASCII fallback. The choice is
    made once from the environment rather than per render.
    """

    reasoning: str
    tick: str
    cross: str
    spinner: str
    bar_full: str
    bar_empty: str


UNICODE_GLYPHS = Glyphs(
    reasoning="⋮ ",
    tick="✓",
    cross="✗",
    spinner="⟳",
    bar_full="▓",
    bar_empty="░",
)
ASCII_GLYPHS = Glyphs(
    reasoning=": ",
    tick="+",
    cross="!",
    spinner="*",
    bar_full="#",
    bar_empty="-",
)


def _declares_unicode() -> bool:
    """Whether the environment claims a UTF-8 capable terminal."""
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = os.environ.get(name)
        if value:
            return "utf" in value.lower()
    return False


GLYPHS = UNICODE_GLYPHS if _declares_unicode() else ASCII_GLYPHS
THINKING_PREFIX = GLYPHS.reasoning


def supports_live_display() -> bool:
    """Whether an in-place live region is safe to draw.

    A redirected or piped stream has no cursor to move, and `rich` will not
    repaint there. Callers fall back to printing each block once, which is also
    what makes the non-interactive path behave predictably in tests.
    """
    try:
        return console.is_terminal and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def confirm_provider_access(
    provider: NamedProvider,
    group: ModelConfig,
    *,
    switching: bool,
) -> bool:
    """Approve one provider and model group for this session only.

    All routing, the credential variable name and the transfer notice are shown
    before any credential is resolved. Without an interactive terminal, or on
    end of input, approval fails closed.
    """
    if not sys.stdin.isatty():
        raise ValueError("Provider approval requires an interactive terminal.")
    # The facts that make the consent meaningful, in the fewest lines: which
    # credential is used, where the conversation goes, and which models that covers.
    # The credential *variable* is named (never its value) because approving a
    # provider is approving the key it reads.
    console.print(
        f"Send this session's conversation to {provider.name!a} at "
        f"{provider.config.base_url}",
        markup=False,
    )
    console.print(
        f"Credential: {provider.config.api_key_env} · group: {group.name!a} · "
        f"{' -> '.join(group.model_ids)}",
        markup=False,
    )
    if switching:
        console.print(
            "Switching providers transfers the full conversation history and "
            "resets session tool-permission grants.",
            style="dim",
        )
    try:
        return Confirm.ask(
            "Trust this provider for this session?", default=False, console=console
        )
    except EOFError:
        return False


def print_provider_selection(provider: NamedProvider, group: ModelConfig) -> None:
    """Show the provider and model this session starts with, in one line.

    The full endpoint and credential variable were already confirmed above; what a
    reader wants on screen is what they are now talking to.
    """
    console.print(
        f"[dim]Model: {group.model_ids[0]} via {provider.name}[/dim]",
        markup=True,
    )


#: The two-column gutter every transcript block starts with. Two is the width every
#: surveyed agent uses, and it is wide enough for a glyph plus a space while keeping
#: the left edge a single straight column.
GUTTER = 2

#: Glyphs that say who is speaking. Role is carried by shape, not by a word: a word
#: at column zero turns the transcript into a table of contents and costs a line per
#: turn, which is why none of the mature agents print a role label.
#:
#: Both are ASCII. A glyph whose display width depends on the terminal's East Asian
#: setting would shift every continuation line by a column on some hosts, and the
#: straight left edge is the whole point of the gutter.
USER_GLYPH = ">"
ASSISTANT_GLYPH = "*"
#: Continuation lines align to the gutter so the left edge stays straight.
CONTINUATION = " " * GUTTER


#: Characters a model id or provider keeps at the narrowest reduction.
NARROW_MODEL_CHARS = 12


def _trim(value: str, limit: int) -> str:
    """``value`` reduced to ``limit`` characters, or unchanged when it already fits."""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _shorten(model: str) -> str:
    """A model id short enough for a narrow terminal.

    The last path segment is the readable part: ``deepseek-v4.1-flash`` means more
    to a reader than the vendor prefix, which ``/model`` shows in full.
    """
    return model.rsplit("/", 1)[-1]


def print_user_message(content: str) -> None:
    """Echo what the operator sent, so the transcript keeps both sides.

    A conversation that shows only replies is unreadable after a few turns: the
    reader cannot tell what was asked, and a steered turn has two user messages
    with nothing to distinguish them from the answers beneath.
    """
    lines = content.split("\n")
    for index, line in enumerate(lines):
        prefix = f"[bold cyan]{USER_GLYPH}[/bold cyan] " if index == 0 else CONTINUATION
        console.print(f"{prefix}{line}", markup=True, highlight=False)
    console.print()


def print_assistant_response(content: str) -> None:
    """Print a reply as one block under a gutter glyph.

    The glyph sits on the first line only: a continuation marker would be copied
    along with code the reader selects.
    """
    console.print(_block(Markdown(content)))


@dataclass(frozen=True, slots=True)
class GutteredBlock:
    """Render a block with a two-column gutter down its left edge.

    A glyph can only lead the *first* line, so the block is rendered to lines and
    prefixed per line rather than concatenated: a Markdown block is not text, and
    wrapping it in a prefix renderable would put the glyph on a line of its own.

    ``glyph`` is empty for a continuation block of the same reply. Repeating it
    mid-reply would read as a new speaker, and a marker on every line would be
    copied along with any code the reader selects.
    """

    renderable: RenderableType
    glyph: str = ""

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> Iterator[Segment]:
        width = max(1, options.max_width - GUTTER)
        lines = console.render_lines(
            self.renderable, options.update(width=width), pad=False
        )
        for index, line in enumerate(lines):
            opens = index == 0 and bool(self.glyph)
            lead = f"{self.glyph} " if opens else CONTINUATION
            yield Segment(lead, Style(dim=True) if opens else None)
            yield from line
            yield Segment.line()


def _block(renderable: RenderableType, *, first: bool = True) -> RenderableType:
    """Place one transcript block under the gutter, glyph first when it opens one."""
    return GutteredBlock(renderable, ASSISTANT_GLYPH if first else "")


def print_banner() -> None:
    console.print(files("cairn.resources").joinpath("banner.txt").read_text("utf-8"))


def _suffix(value: object, prefix: str = " ") -> str:
    """Render a model-supplied argument only when it really is a string."""
    return f"{prefix}{value}" if isinstance(value, str) else ""


# The label a call was described with, keyed by the call it belongs to. Runtime
# owns the call/result pairing and reports both ids; this only remembers how the
# call was worded so its result can repeat it. Entries are removed when their
# result arrives, and a result without a matching call falls back to the tool
# name rather than showing another call's arguments.
_pending_tool_labels: dict[str, str] = {}


def _tool_name(event: Event) -> str:
    raw_tool = event.data.get("tool")
    return raw_tool if isinstance(raw_tool, str) else "tool"


def _describe_tool_call(event: Event) -> str:
    """One line identifying the tool and the argument that explains it."""
    tool = _tool_name(event)
    raw_arguments = event.data.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}

    if tool == "read_file":
        end = arguments.get("end_line")
        if end is None:
            return f"read_file{_suffix(arguments.get('path'))}"
        start = arguments.get("start_line", 1)
        return f"read_file{_suffix(arguments.get('path'))} lines {start}-{end}"
    if tool == "edit_file":
        return f"edit_file{_suffix(arguments.get('path'))}"
    if tool == "bash":
        return f"bash{_suffix(arguments.get('command'), ': ')}"
    # Any other tool still has to say what it ran on, or the line identifies
    # nothing. The first short string argument is the closest thing to a subject.
    for value in arguments.values():
        if isinstance(value, str) and value and len(value) <= MAX_ARGUMENT_CHARS:
            return f"{tool}{_suffix(value, ': ')}"
    return tool


def _take_tool_label(event: Event) -> str:
    """The label the matching call was described with, if it is still pending.

    Keyed by the call id runtime reports on both events, so a second call to the
    same tool cannot be described with the first one's arguments.
    """
    call_id = event.data.get("tool_call_id")
    if not isinstance(call_id, str):
        return ""
    return _pending_tool_labels.pop(call_id, "")


def _render_tool_result(event: Event) -> None:
    """Report a finished tool call as one line.

    A call and its result are one fact, so they share a line instead of
    occupying two with a blank line between them. The exit code stays: a status
    line that cannot distinguish a normal failure from a killed process is
    hiding the number a reader needs. Failures add a bounded diagnostic beneath,
    because that is the part a reader acts on; the full record stays in the trace
    for `/trace`.
    """
    tool = _tool_name(event)
    label = _take_tool_label(event) or tool
    exit_code = event.data["exit_code"]
    raw_stdout = event.data.get("stdout")
    stdout = raw_stdout if isinstance(raw_stdout, str) else ""
    raw_stderr = event.data.get("stderr")
    stderr = raw_stderr if isinstance(raw_stderr, str) else ""
    mark = "✓" if exit_code == 0 else "✗"

    if exit_code == 0 and tool == "edit_file":
        detail = stdout.strip()
        if detail:
            console.print(f"{mark} {detail}", style="green", markup=False)
            return
    if exit_code == 0:
        console.print(f"{mark} {label}", style="green", markup=False)
        return

    console.print(f"{mark} {label} (exit {exit_code})", style="bold red", markup=False)
    diagnostic = (stderr or stdout).strip()
    if diagnostic:
        console.print(_indented(_bounded(diagnostic)), style="yellow", markup=False)


def _bounded(text: str, limit: int = MAX_DIAGNOSTIC_LINES) -> str:
    """Keep a diagnostic to what a reader can act on.

    A failing command can produce megabytes of stderr; printing all of it pushes
    the rest of the session out of the scrollback. The trace keeps the full text.
    """
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    omitted = len(lines) - limit
    return "\n".join([*lines[:limit], f"… {omitted} more lines (see /trace)"])


def _indented(text: str) -> str:
    """Indent a diagnostic so it reads as detail under its own status line."""
    return "\n".join(f"  {line}" for line in text.splitlines())


def _format_token_count(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return "unknown"


def _usage_suffix(event: Event) -> str:
    """The turn's token totals, or nothing when the trace carries no usage.

    An absent `usage` key means the turn never measured tokens. Spelling that
    out as "input unknown" beside a failure note adds a second unknown to a line
    whose one fact is the failure.
    """
    usage = event.data.get("usage")
    if not isinstance(usage, dict) or not usage:
        return ""

    raw_input_tokens = usage.get("input_tokens")
    raw_output_tokens = usage.get("output_tokens")
    input_tokens = _format_token_count(raw_input_tokens)
    output_tokens = _format_token_count(raw_output_tokens)
    return f" · tokens: input {input_tokens}, output {output_tokens}"


def _reasoning_width() -> int:
    """The columns one reasoning line occupies beside its marker."""
    return max(20, console.width - len(THINKING_PREFIX))


def _wrap(line: str, width: int) -> list[str]:
    """Split one reasoning line into the rows it occupies."""
    stripped = line.strip()
    if not stripped:
        return []
    rows = [stripped[start : start + width] for start in range(0, len(stripped), width)]
    return rows


def _reasoning_text_lines(chunks: list[str]) -> list[str]:
    """The reasoning as logical lines, before wrapping to the terminal."""
    return "".join(chunks).splitlines()


def _rows_for_lines(lines: list[str], *, growing: bool) -> list[tuple[str, bool]]:
    """Rows for these lines, wrapped; a trailing growing line is marked so.

    Wrapping here rather than leaving it to the console is what makes a line and
    a rendered row the same thing, so a log and the on-screen window agree on
    what "a line" is. Only the trailing line can still be written, so only it is
    ever marked growing.
    """
    width = _reasoning_width()
    rows: list[tuple[str, bool]] = []
    for index, line in enumerate(lines):
        wrapped = _wrap(line, width)
        if not wrapped:
            continue
        is_growing = growing and index == len(lines) - 1
        rows.extend((row, False) for row in wrapped[:-1])
        rows.append((wrapped[-1], is_growing))
    return rows


def _reasoning_rows(chunks: list[str], *, final: bool) -> list[tuple[str, bool]]:
    """The whole reasoning as rows, for the window that renders it live."""
    lines = _reasoning_text_lines(chunks)
    unterminated = not "".join(chunks).endswith(("\n", "\r"))
    settled = lines if final or not unterminated else lines[:-1]
    rows = _rows_for_lines(settled, growing=False)
    if unterminated and not final:
        rows.extend(_rows_for_lines(lines[-1:], growing=True))
    return rows


def _render_row(text: str, *, growing: bool) -> str:
    """One printable reasoning row; a growing row erases itself first.

    Rewriting a growing row in place is what this display stopped doing with
    hand-drawn cursor moves. Padding it to the full width and returning to
    column zero is the one move that needs no row arithmetic: it stays on the
    row it drew, so it cannot reach a neighbour, and the next delta overwrites
    what is there. A file reads the padding as blank space.
    """
    if not growing:
        return text
    return f"{text:<{_reasoning_width()}}\r"


def _write_reasoning_rows(rows: list[tuple[str, bool]]) -> int:
    """Write reasoning rows to a redirected stream; say how many landed.

    This path exists for readers of a file or a pipe: no live region will ever
    repaint it, so the bytes have to stand on their own. It is plain text on
    stdout beside the answer, and guarded, because a closed stream is a reader's
    choice and not a display failure the turn must clean up after.
    """
    if not rows:
        return 0
    rendered = "".join(
        f"{THINKING_PREFIX}{_render_row(text, growing=growing)}\n"
        for text, growing in rows
    )
    try:
        sys.stdout.write(rendered)
        sys.stdout.flush()
    except (BrokenPipeError, ValueError):
        return 0
    return len(rows)


def _reasoning_window() -> int:
    """How many trailing reasoning lines the screen can carry.

    A fixed count either crowds a short terminal, where the answer stops being
    visible while the model thinks, or wastes a tall one. The window is a share
    of the screen, bounded so it neither collapses nor grows without limit.
    """
    height = console.height
    if height <= 0:
        return MAX_THINKING_LINES
    return min(
        MAX_THINKING_LINES, max(MIN_THINKING_LINES, height // THINKING_SCREEN_SHARE)
    )


def _split_blocks(text: str) -> tuple[str, str]:
    """Split Markdown into what can be printed for good and what is still open.

    Only a blank line outside ``` ends a block. A blank line inside a fence does
    not, and neither does a fence, open or closed: a code block that has closed
    is printable as a whole, but an open one keeps changing height with every
    line appended, so redrawing it repaints the whole block each frame. It
    therefore stays in the pending tail until it closes, while the prose before
    it keeps settling.
    """
    end = _last_settled_offset(text)
    return text[:end], text[end:]


def _last_settled_offset(text: str) -> int:
    """Where the printable prefix ends: after the last blank line outside a fence.

    The final line does not count even when blank: a block is only settled once
    something follows it.
    """
    lines = text.splitlines(keepends=True)
    fenced = False
    end = 0
    offset = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and not line.strip() and index + 1 < len(lines):
            end = offset + len(line)
        offset += len(line)
    return end


def format_status_line(
    *,
    active: bool,
    tokens_per_second: float | None = None,
    context_fraction: float | None = None,
    cached_fraction: float | None = None,
    reasoning_tokens: int | None = None,
    trace_id: str | None = None,
    step: tuple[int, int] | None = None,
) -> Text:
    """One quiet line describing what the turn is doing.

    Only the signals a reader acts on appear, and each is omitted rather than
    shown as a placeholder when it has no value: a dash where a number belongs
    is noise that never changes. Color is never the only signal — the spinner
    and the filled bar segments carry the same information in text.
    """
    parts: list[Text] = []
    spinner = GLYPHS.spinner if active else GLYPHS.tick
    parts.append(Text(spinner, style="green" if not active else "cyan"))

    if step is not None:
        # Progress belongs on this line rather than in the transcript. Printing one
        # line per step filled the scrollback with a hundred lines that said nothing
        # once the turn ended.
        current, total = step
        parts.append(Text(f" step {current}/{total}", style="dim"))

    if tokens_per_second is not None and tokens_per_second > 0:
        parts.append(Text(f" {tokens_per_second:.0f} tok/s", style="dim"))

    if context_fraction is not None:
        filled = round(max(0.0, min(1.0, context_fraction)) * _BAR_WIDTH)
        bar = GLYPHS.bar_full * filled + GLYPHS.bar_empty * (_BAR_WIDTH - filled)
        parts.append(Text(f"  ctx {bar} {context_fraction * 100:.0f}%", style="dim"))

    if cached_fraction is not None and cached_fraction > 0:
        parts.append(Text(f"  {cached_fraction * 100:.0f}% cached", style="dim"))

    if reasoning_tokens:
        parts.append(Text(f"  {reasoning_tokens} reasoning", style="dim"))

    if trace_id:
        # The prefix of the turn's trace, so an identifier is visible without
        # printing a whole line for it. `/trace` accepts the same prefix.
        parts.append(Text(f"  {trace_id[:8]}", style="dim"))

    return Text.assemble(*parts)


def _count(value: object) -> int:
    """Read a non-negative integer count, defaulting to zero."""
    if type(value) is int and value >= 0:
        return value
    return 0


def _plain_text(event: Event) -> str:
    """Model text stripped of anything that could drive the terminal.

    Rich markup and C0/C1 control characters have no authority over display.
    Newline and tab survive, as does printable Unicode including multibyte
    fragments split across deltas.
    """
    text = str(event.data["text"])
    return "".join(
        char
        for char in text
        if char in "\n\t" or not (ord(char) < 32 or 127 <= ord(char) <= 159)
    )


def console_event_handler(event: Event, *, report_trace_failure: bool = True) -> None:
    """Render the events the streaming display leaves to the console.

    `report_trace_failure` is the one policy a caller owns: a durable turn
    reports an unstored trace itself, and this handler must not repeat it.
    """
    match event.type:
        case "context_trimmed":
            omitted_turns = event.data["omitted_turns"]
            omitted_messages = event.data["omitted_messages"]
            console.print(
                f"context: omitted {omitted_turns} older turns "
                f"({omitted_messages} messages); full history retained.",
                style="dim",
                markup=False,
            )

        case "tool_call":
            # A call is not a fact on its own; remember how to describe it so the
            # result can report both on one line. The result event carries no
            # arguments, so this is the only place they are available.
            raw_call_id = event.data.get("tool_call_id")
            if isinstance(raw_call_id, str):
                _pending_tool_labels[raw_call_id] = _describe_tool_call(event)

        case "tool_result":
            _render_tool_result(event)

        case "tool_error":
            label = _take_tool_label(event) or str(event.data["tool"])
            console.print(
                f"✗ {label}: {event.data['error']}", style="bold red", markup=False
            )

        case "tool_denied":
            label = _take_tool_label(event) or str(event.data["tool"])
            console.print(
                f"✗ {label}: {event.data['error']}", style="bold red", markup=False
            )

        case "agent_finish":
            console.print("[dim]✓ done[/dim]")

        case "agent_budget_exhausted":
            reason = event.data["reason"]
            limit = event.data["limit"]
            used = event.data["used"]

            if reason == "max_steps":
                console.print(
                    f"[bold yellow]"
                    f"Agent stopped: step budget exhausted "
                    f"({used}/{limit})."
                    f"[/bold yellow]"
                )

        case "trace_finish":
            # The identifier and usage are on every turn; printing them made the
            # scrollback read like a log. `/trace` lists them on demand, so only a
            # turn whose trace could not be stored speaks up here.
            if report_trace_failure and not event.data.get("persisted", True):
                error = event.data.get("persistence_error")
                console.print(
                    f"[yellow]trace unavailable: persistence failed ({error})"
                    f"{_usage_suffix(event)}[/yellow]"
                )


#: Builds a live region. The callable takes the function that produces the current
#: frame, so a host can ask for fresh content whenever it repaints.
LiveFactory = Callable[["Callable[[], RenderableType]"], LiveRegion]


class _RichLiveRegion:
    """A live region drawn by ``rich``, for a terminal nobody else owns."""

    def __init__(self, frame: "Callable[[], RenderableType]") -> None:
        self._frame = frame
        # The rich object, not the protocol: ``Live.start`` takes no renderable, so
        # this adapter's own signature differs from the region it implements.
        self._rich: Live | None = None

    def start(self, renderable: RenderableType) -> None:
        # transient: each block is redrawn here, then printed once for good when
        # it settles, so the live copy must not survive the stop.
        self._rich = Live(
            renderable,
            console=console,
            refresh_per_second=8,
            vertical_overflow="visible",
            transient=True,
        )
        self._rich.start()

    def update(self, renderable: RenderableType) -> None:
        if self._rich is not None:
            self._rich.update(renderable)

    def stop(self) -> None:
        if self._rich is not None:
            self._rich.stop()
            self._rich = None


def rich_live_region(frame: "Callable[[], RenderableType]") -> LiveRegion:
    """The default host: a cursor-moving region in a plain terminal."""
    return _RichLiveRegion(frame)


class StreamingDisplay:
    """Per-CLI transient display state; it never advances an Agent lifecycle.

    Reasoning and answer text arrive as separate deltas. Reasoning is shown as a
    short block of trailing lines under a dim marker and is replaced as it grows,
    so a long chain of thought does not fill the scrollback.

    Answer text streams as Markdown. Completed blocks are printed once and stay
    in the scrollback; only the block still being written is redrawn. That is
    what makes the answer readable while it arrives instead of after: a finished
    paragraph keeps its formatting, and the live region costs one paragraph, not
    the whole message.

    Both live threads share one Live region, because `rich` only paints the top
    of its live stack and hand-drawn cursor moves cannot keep two redraw areas
    aligned once other output interleaves. The same rate limit that throttles
    the status line throttles the reasoning repaint.
    """

    def __init__(
        self,
        *,
        live_factory: "LiveFactory | None" = None,
        on_status: "Callable[[str], None] | None" = None,
    ) -> None:
        self.final_text_visible = False
        # Where the live region is painted. Injected so the display never has to
        # know whether it is drawing into a plain terminal or an application that
        # owns the bottom of the screen.
        self._live_factory: LiveFactory = (
            live_factory if live_factory is not None else rich_live_region
        )
        self._reasoning: list[str] = []
        self._reasoning_open = False
        # The temporary reasoning preview; it owns what the reader sees while the
        # model thinks, and the receipt that survives it.
        self._thinking = ThinkingPreview()
        # Off terminal, reasoning is appended rather than redrawn. This counts
        # the logical lines already written so a delta adds only what is new.
        self._logged_lines = 0
        self._answer: list[str] = []
        self._settled_blocks = 0
        self._settled_chars = 0
        self._live: LiveRegion | None = None
        # Counters for the status line. Updated on every delta; the line itself
        # is repainted on a timer, so a burst of deltas costs one repaint.
        self._turn_started: float | None = None
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._reasoning_tokens: int | None = None
        self._trace_id: str | None = None
        self._step: tuple[int, int] | None = None
        self._model_name = ""
        self._provider_name = ""
        #: Columns the status may use. Set by the caller that knows the terminal.
        self._status_width: int | None = None
        #: The last turn's rate, kept so an idle session does not show it decaying.
        self._frozen_rate: float | None = None
        # Where the session facts are shown when the caller wants them below the
        # input rather than on the live region.
        self._on_status = on_status
        self._context_used: int | None = None
        self._context_limit: int | None = None
        self._status_painted = 0.0
        self._text_seen = False
        self._active = False
        self._completed_text_step: int | None = None

    def reset(self) -> None:
        """Return to a clean slate between turns.

        Closing the live region first matters: a cancelled attempt can leave one
        open, and a region nobody stopped would keep repainting over the next
        turn's output.
        """
        self._close_live()
        self._forget_attempt()
        # The running total belongs to the turn that just ended. The context
        # limit belongs to the session, so it survives: the next turn is measured
        # against the same budget.
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._reasoning_tokens = None
        self._step = None
        self._thinking.clear()

    def _forget_attempt(self) -> None:
        """Drop everything the display knows about the attempt so far.

        One owner for that state: `reset()` and a new stream start both need it,
        and a field that only one of them cleared would leak into the next turn.
        """
        self.final_text_visible = False
        self._completed_text_step = None
        self._reasoning = []
        self._reasoning_open = False
        self._logged_lines = 0
        self._answer = []
        self._settled_blocks = 0
        self._settled_chars = 0
        self._status_painted = 0.0
        self._text_seen = False
        self._active = False

    def observe(self, event: Event) -> bool:
        """Return True only for handled streaming events; legacy output stays put."""
        handler = _STREAMING_HANDLERS.get(event.type)
        if handler is None:
            self._note_agent_finish(event)
            return False
        handler(self, event)
        return True

    def _note_step(self, event: Event) -> None:
        """Remember how far through its step budget the turn is.

        Progress is shown on the status line rather than printed, so a turn with a
        hundred steps leaves no hundred lines behind.
        """
        step = event.data.get("step")
        total = event.data.get("max_steps")
        if type(step) is int and type(total) is int:
            self._step = (step, total)
            self._refresh_status()

    def _open_stream(self, event: Event) -> None:
        """Start a fresh attempt: nothing from the previous one may survive."""
        self._forget_attempt()
        self._turn_started = perf_counter()
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._reasoning_tokens = None
        self._trace_id = None
        self._active = True

    def observe_context(self, *, used: int | None, limit: int | None) -> None:
        """Adopt the context measurement of the request about to be sent.

        This is what makes the gauge live during a turn. It is fed from the
        request the runtime is about to build, because that is the only point
        where "how full is the context" is both known and about to matter.
        """
        self._context_used = used if used is not None else self._context_used
        self._context_limit = limit if limit is not None else self._context_limit

    def observe_step_usage(
        self, *, input_tokens: int | None, output_tokens: int | None
    ) -> None:
        """Add one step's measured usage to the running turn total.

        A step that reports no usage adds nothing and erases nothing: a timed-out
        or cancelled call must not discard the figures its predecessors measured,
        which is what made a long turn display no numbers at all.
        """
        if input_tokens is not None:
            self._input_tokens += input_tokens
        if output_tokens is not None:
            self._output_tokens += output_tokens

    def _show_text_delta(self, event: Event) -> None:
        self._text_seen = True
        self._answer.append(_plain_text(event))
        self._render_answer_progress()

    def _show_reasoning_delta(self, event: Event) -> None:
        text = _plain_text(event)
        self._reasoning.append(text)
        self._thinking.append(text)
        self._render_reasoning()

    def _finish_stream(self, event: Event) -> None:
        status = event.data["status"]
        if status != "completed":
            self.close_incomplete(abandoned=status == "abandoned")
            return
        self._active = False
        self._close_reasoning()
        if not self._text_seen:
            return
        self._completed_text_step = event.data.get("step")
        self.final_text_visible = True
        self._render_answer()

    def _record_turn_totals(self, event: Event) -> None:
        """Take the turn's final totals; the status line then stops spinning."""
        raw_trace_id = event.data.get("trace_id")
        self._trace_id = raw_trace_id if isinstance(raw_trace_id, str) else None
        usage = event.data.get("usage")
        if isinstance(usage, dict):
            self._input_tokens = _count(usage.get("input_tokens"))
            self._output_tokens = _count(usage.get("output_tokens"))
            self._cached_tokens = _count(usage.get("cached_tokens"))
            reasoning = _count(usage.get("reasoning_tokens"))
            self._reasoning_tokens = reasoning or None
        self._active = False
        # Freeze the rate now: leaving it live would make it decay as the idle clock
        # runs, which reads as the model slowing down after it has finished.
        self._frozen_rate = self._rate()
        self._refresh_status()

    def _note_agent_finish(self, event: Event) -> None:
        """Remember whether this step's text is already on screen."""
        if event.type != "agent_finish":
            return
        self.final_text_visible = (
            self._completed_text_step is not None
            and self._completed_text_step == event.data.get("step")
        )

    def _refresh_status(self) -> None:
        """Publish the status area, whether or not a live region is open.

        The status has its own destination, so it is not gated on the live region.
        Gating it meant the final figures never appeared: the last turn's usage and
        its frozen rate become known exactly when the live region closes, which was
        the moment the guard stopped anything from being painted.

        Rate limited because the status changes many times a second and each repaint
        costs a redraw; the limit applies to the paint, not to what is published.
        """
        now = perf_counter()
        if now - self._status_painted < STATUS_INTERVAL_SECONDS:
            return
        self._status_painted = now
        if self._on_status is not None:
            self._on_status("\n".join(self.status_lines(width=self._status_width)))
        if self._live is not None:
            frame = self._live_renderable()
            if frame is not None:
                self._live.update(frame)

    def _render_reasoning(self) -> None:
        """Show the reasoning; live where a live region is safe, else a log.

        In a terminal the block rides the shared live region, whose row count
        `rich` computes from the actual render, so a repaint erases exactly
        what it drew and interleaved tool output cannot shift the target.

        Off terminal there is nothing to repaint, and a redraw would mean escape
        bytes in a file. Each line is written once, when it settles, and the one
        line still being written overwrites itself in place.
        """
        if not self._reasoning_open:
            # The preview's clock starts with the first reasoning delta, so the
            # receipt measures thinking rather than the turn.
            self._thinking.begin(at=perf_counter())
        self._reasoning_open = True
        if supports_live_display():
            self._show_live_reasoning()
            return
        self._log_new_reasoning()

    def _show_live_reasoning(self) -> None:
        """Redraw the reasoning inside the one live region this turn owns."""
        if self._live is None:
            self._start_live()
        else:
            self._refresh_status()

    def _log_new_reasoning(self, *, final: bool = False) -> None:
        """Write the reasoning a redirected stream has not seen yet.

        Only the lines past `_logged_lines` are wrapped and written. Wrapping the
        whole reasoning on every delta would make a long chain of thought
        quadratic on the log path. The line still being written is rewritten in
        place by `_render_row`'s padding, then settled once when it ends.
        """
        lines = _reasoning_text_lines(self._reasoning)
        # A trailing newline leaves no empty final element, so a line is still
        # being written only when the text does not end with one.
        unterminated = not "".join(self._reasoning).endswith(("\n", "\r"))
        settled = lines if final or not unterminated else lines[:-1]
        if len(settled) > self._logged_lines:
            fresh = settled[self._logged_lines :]
            _write_reasoning_rows(_rows_for_lines(fresh, growing=False))
            self._logged_lines = len(settled)
        if unterminated and not final:
            # The line in flight is rewritten in place on every delta, so it is
            # not settled here: only its final form goes to the log.
            _write_reasoning_rows(_rows_for_lines(lines[-1:], growing=True))

    def status_lines(self, *, width: int | None = None) -> list[str]:
        """The session facts, as the two lines a reader scans while waiting.

        Two lines rather than one because they answer different questions and age
        differently: what this session is talking to rarely changes, while the
        numbers change every second. Packing both onto one line made a long line
        that was mostly stale.

        ``width`` makes them fit. A status that overflows wraps into the composer or
        is cut with no rule about what was lost, so the reduction order is fixed and
        stated once here: shorten the model, then drop context, then drop usage. The
        provider and model never both disappear — a reader who cannot tell what they
        are talking to has lost the most important fact on the line.
        """
        for reduction in ("full", "short-model", "no-context", "narrow"):
            lines = self._status_at(reduction)
            if width is None or max(len(line) for line in lines) <= width:
                return lines
        return lines

    def _status_at(self, reduction: str) -> list[str]:
        """Both status lines at one reduction level."""
        model = _shorten(self._model_name) if reduction != "full" else self._model_name
        if reduction == "narrow":
            # The last resort: keep identity readable by dropping the version, and
            # never drop both parts — a reader who cannot tell what they are talking
            # to has lost the most important fact on the line.
            model = _trim(model, NARROW_MODEL_CHARS)
            provider = _trim(self._provider_name, NARROW_MODEL_CHARS)
        else:
            provider = self._provider_name
        first = "  ".join(part for part in (model, provider) if part)

        measured = bool(self._output_tokens or self._input_tokens)
        parts: list[str] = []
        if measured:
            parts.append(f"{self._input_tokens} in / {self._output_tokens} out")
            rate = self._rate()
            if rate is not None:
                parts.append(f"{rate:.0f} tok/s")
        keeps_context = reduction in ("full", "short-model")
        if keeps_context and self._context_used and self._context_limit:
            parts.append(f"ctx {self._context_used * 100 // self._context_limit}%")
        if not parts:
            # Nothing measured yet: say so plainly rather than showing zeros that
            # look like a measurement.
            parts.append("ready")
        return [first, "  ".join(parts)]

    def _rate(self) -> float | None:
        """Tokens per second for the turn, frozen once it ends.

        A live rate that keeps recomputing after the turn falls as the idle clock
        runs, which reads as the model getting slower for no reason. The last turn's
        rate is the useful figure, so it stops moving when the turn does.
        """
        if self._frozen_rate is not None:
            return self._frozen_rate
        if not self._turn_started or not self._output_tokens:
            return None
        elapsed = max(0.0, perf_counter() - self._turn_started)
        return self._output_tokens / elapsed if elapsed > 0 else None

    def status_text(self) -> str:
        """The status as one string, for callers that place a single line."""
        return " · ".join(part for part in self.status_lines() if part)

    def set_status_width(self, width: int | None) -> None:
        """Tell the display how wide its status may be.

        The display owns what the status says and the caller owns how much room
        there is, so the width is pushed in rather than guessed here.
        """
        self._status_width = width
        self._refresh_status()

    def observe_model(self, name: str, *, provider: str = "") -> None:
        """Remember which model and provider this session talks to."""
        self._model_name = name
        self._provider_name = provider

    def reveal_reasoning(self) -> None:
        """Widen the live preview, if one is running.

        Reasoning is not kept after the turn settles, so there is nothing to reveal
        then and the key does nothing. That is why the hint is only shown while the
        preview runs: a key that silently does nothing is worse than no key.
        """
        if self._thinking.expand():
            self._refresh_status()

    def _close_reasoning(self) -> None:
        """Settle the reasoning: clear the preview and leave one receipt.

        The text is *not* written to the scrollback. It was already temporary while
        it streamed, and reprinting it on settle is what made a turn cost a
        screenful: the whole argument arrived, then stayed, next to the answer that
        summarised it. The reasoning itself belongs to the trace; what the transcript
        keeps is the fact that thinking happened and how long it took.
        """
        if not self._reasoning_open:
            return
        self._thinking.finish(at=perf_counter())
        self._close_live()
        # The receipt belongs to the transcript, not to the redirected log: it is the
        # one line a reader of the conversation keeps. It goes through the console so
        # it appears wherever the answer will.
        receipt = self._thinking.receipt() if self._thinking.has_content() else ""
        if receipt:
            console.print(Text(receipt, style="dim"))
        self._reasoning_open = False

    def _render_answer_progress(self) -> None:
        """Print each finished Markdown block once; redraw only the last one.

        A block ends at a blank line outside a fenced code block. Printing a
        block the moment it is complete keeps finished prose formatted and in the
        scrollback, while the live region carries only the paragraph still being
        written, so the redraw cost does not grow with the answer.

        `_settled_chars` records how much of the answer has already been printed.
        Without it every delta would reprint the finished prefix, because the
        split would keep reporting the same completed blocks.
        """
        text = "".join(self._answer)
        settled, pending = _split_blocks(text)
        if len(settled) > self._settled_chars:
            fresh = settled[self._settled_chars :]
            self._settled_chars = len(settled)
            self._close_live()
            console.print(_block(Markdown(fresh), first=not self._settled_blocks))
            self._settled_blocks += 1
        if not pending.strip():
            self._close_live()
            return
        if not supports_live_display():
            # A redirected stream has no cursor to move. Emit finished blocks
            # only, so a reader of a log file sees each block once.
            return
        if self._live is None:
            self._start_live()
        else:
            self._live.update(self._live_renderable())

    def _start_live(self) -> None:
        """Open the one live region this turn: pending block, status, reasoning.

        The region is whatever host the caller supplied. ``rich.Live`` is the
        default and keeps the cursor-moving behaviour a plain terminal expects; the
        CLI substitutes a host that paints inside its own bottom viewport, which is
        what lets output stay selectable while the input keeps its position.
        """
        self._live = self._live_factory(self._live_renderable)
        self._live.start(self._live_renderable())
        self._status_painted = perf_counter()

    def _live_renderable(self) -> Group:
        """The block still being written, the status line, and trailing reasoning.

        One renderable, not two live regions: `rich` only paints the top of its
        live stack, so a second Live for the status line would never appear.
        """
        body = Markdown("".join(self._answer)[self._settled_chars :])
        return Group(body, Text(""), self._status_line(), self._reasoning_renderable())

    def _reasoning_renderable(self) -> Text:
        """The temporary reasoning preview: a header and its newest two rows.

        Only the newest rows are shown, because a reader looks at what the model is
        doing now. The key hint is appended only while the preview is running: once
        the turn settles there is nothing left to reveal, and promising a key that
        does nothing is worse than saying nothing.
        """
        if not self._thinking.has_content():
            return Text("")
        lines = [
            self._thinking.header(),
            *(f"  {row}" for row in self._thinking.visible()),
        ]
        hint = self._thinking.hint()
        if hint:
            lines.append(f"  {hint}")
        return Text("\n".join(lines), style="dim")

    def _status_line(self) -> Text:
        elapsed = (
            max(0.0, perf_counter() - self._turn_started) if self._turn_started else 0.0
        )
        tps = (
            self._output_tokens / elapsed
            if elapsed > 0 and self._output_tokens
            else None
        )
        context_fraction = (
            self._context_used / self._context_limit
            if self._context_limit and self._context_used is not None
            else None
        )
        cached_fraction = (
            self._cached_tokens / self._input_tokens
            if self._input_tokens and self._cached_tokens
            else None
        )
        return format_status_line(
            active=self._active,
            step=self._step,
            tokens_per_second=tps,
            context_fraction=context_fraction,
            cached_fraction=cached_fraction,
            reasoning_tokens=self._reasoning_tokens,
            trace_id=self._trace_id,
        )

    def _close_live(self) -> None:
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    def _render_answer(self) -> None:
        """Finish the answer: settle the last block and close the live region."""
        text = "".join(self._answer).strip()
        if not text:
            return
        _, pending = _split_blocks("".join(self._answer))
        if pending.strip():
            self._close_live()
            console.print(_block(Markdown(pending), first=not self._settled_blocks))
            self._settled_blocks += 1
            return
        self._close_live()
        console.print(_block(Markdown(text), first=not self._settled_blocks))
        self._settled_blocks += 1

    def close_incomplete(self, *, abandoned: bool = False) -> None:
        """Abandon an attempt: drop partial output and say so.

        An unfinished markdown block is discarded rather than printed, because a
        half-written table or code fence reads as corruption once it stops
        changing.
        """
        self._close_live()
        self._discard_attempt_output()
        self._say_attempt_failed(abandoned=abandoned)

    def _discard_attempt_output(self) -> None:
        """Drop what the attempt produced, keeping what the turn knows.

        `_forget_attempt` is the other half: it also forgets the turn's counters.
        This one keeps the active flag and the completed step, because the caller
        still has to report the outcome it just established.
        """
        self._answer = []
        self._reasoning = []
        self._settled_blocks = 0
        self._settled_chars = 0
        self._completed_text_step = None

    def _say_attempt_failed(self, *, abandoned: bool) -> None:
        """Separate the warning from the reasoning it follows, then report it.

        Only a terminal needs the separation: there the reasoning may still be
        the last thing on screen. A redirected stream already ended its reasoning
        with a newline, and its warning goes to a different channel, so a blank
        line there is noise in a log.
        """
        if self._reasoning_open:
            self._reasoning_open = False
            if supports_live_display():
                console.print()
        if not self._active:
            return
        self._active = False
        console.print(
            "[attempt abandoned; incomplete output discarded]"
            if abandoned
            else "[response incomplete; incomplete output discarded]",
            style="yellow",
            markup=False,
        )


# Streaming event type -> the method that presents it. A table keeps the
# dispatch readable and lets every event type name its one handler.
_STREAMING_HANDLERS: dict[str, Callable[["StreamingDisplay", Event], None]] = {
    "agent_step": StreamingDisplay._note_step,
    "model_stream_start": StreamingDisplay._open_stream,
    "model_text_delta": StreamingDisplay._show_text_delta,
    "model_reasoning_delta": StreamingDisplay._show_reasoning_delta,
    "model_stream_end": StreamingDisplay._finish_stream,
    "trace_finish": StreamingDisplay._record_turn_totals,
}


def confirm_workspace_trust(workspace_root: Path) -> bool:
    """Ask once whether this session may read and write the started directory.

    Cairn edits files in the directory it was started in, so that directory is
    trusted explicitly before the first turn. Without an interactive terminal or
    on end of input, trust fails closed.
    """
    if not sys.stdin.isatty():
        raise ValueError("Workspace trust requires an interactive terminal.")
    # The consent question, and the one fact that makes it meaningful. The rest of
    # the boundary is documented in the sandbox guide; repeating it here delayed the
    # first turn without changing the decision.
    console.print(f"\nWork in {workspace_root}?\n", markup=False)
    try:
        return Confirm.ask(
            "Trust this directory for this session?", default=False, console=console
        )
    except EOFError:
        return False


def console_permission_prompt(request: PermissionRequest) -> PermissionChoice:
    console.print("\n[bold yellow]Permission required[/bold yellow]\n")
    console.print(f"Capability: {request.capability.value}", markup=False)

    command = request.tool_call.arguments.get("command")
    if isinstance(command, str):
        console.print(f"Command: {command}", markup=False)

    # A filesystem request names the exact directories it would expose, so the
    # operator approves a concrete path set rather than an abstract capability.
    for path in request.paths:
        console.print(f"Path: {path}", markup=False)

    console.print(f"Reason: {request.justification}\n", markup=False)
    console.print(
        "[1] Allow once\n"
        f"[2] Allow {request.capability.value} for this session\n"
        "[3] Deny",
        markup=False,
    )
    try:
        choice = Prompt.ask(
            "Choice", choices=["1", "2", "3"], default="3", console=console
        )
    except EOFError:
        # No interactive input left: deny rather than let EOF escape as an
        # exception and abort the turn.
        return PermissionChoice.DENY
    return {
        "1": PermissionChoice.ALLOW_ONCE,
        "2": PermissionChoice.ALLOW_SESSION,
        "3": PermissionChoice.DENY,
    }[choice]


def print_runtime_error(exc: Exception) -> None:
    console.print(f"✗ {type(exc).__name__}: {exc}", style="bold red", markup=False)
