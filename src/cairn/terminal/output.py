import os
import sys
from dataclasses import dataclass
from importlib.resources import files
from time import perf_counter

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt
from rich.text import Text

from cairn.core.events import Event
from cairn.core.model_manager import ModelConfig
from cairn.core.permissions import PermissionChoice, PermissionRequest
from cairn.core.provider_catalog import NamedProvider

console = Console()

# A tool argument long enough to dominate its status line is summarised by the
# tool name alone; the full value is in the trace.
MAX_ARGUMENT_CHARS = 120
# Failure output beyond this many lines moves to the trace instead of the screen.
MAX_DIAGNOSTIC_LINES = 20
# How many trailing reasoning lines stay on screen. The model can reason for
# thousands of tokens; a reader wants the current thread, not the transcript.
MAX_THINKING_LINES = 6
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
    console.print("Project model configuration requests access to a credential.")
    console.print(f"Provider: {provider.name!a}", markup=False)
    console.print(f"Endpoint: {provider.config.base_url!a}", markup=False)
    console.print(f"Credential variable: {provider.config.api_key_env!a}", markup=False)
    console.print(f"Model group: {group.name!a}", markup=False)
    for position, model_id in enumerate(group.model_ids, start=1):
        console.print(f"  {position}. {model_id!a}", markup=False)
    console.print(
        "The selected API key and conversation content will be sent to this endpoint."
    )
    if switching:
        console.print(
            "Switching providers transfers the full conversation history to that "
            "endpoint and resets session tool-permission grants."
        )
    try:
        return Confirm.ask(
            "Trust this provider for this session?", default=False, console=console
        )
    except EOFError:
        return False


def print_provider_selection(provider: NamedProvider, group: ModelConfig) -> None:
    """Show the provider and model group this session starts with."""
    console.print(f"Provider: {provider.name!a}", markup=False)
    console.print(
        f"Model group: {group.name!a} ({' -> '.join(group.model_ids)})", markup=False
    )


def print_assistant_response(content: str) -> None:
    console.print("[bold]Cairn>[/bold]")
    console.print(Markdown(content))
    console.print()


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
    usage = event.data.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    raw_input_tokens = usage.get("input_tokens")
    raw_output_tokens = usage.get("output_tokens")
    input_tokens = _format_token_count(raw_input_tokens)
    output_tokens = _format_token_count(raw_output_tokens)
    return f" · tokens: input {input_tokens}, output {output_tokens}"


def _split_blocks(text: str) -> tuple[str, str]:
    """Split Markdown into what can be printed for good and what is still open.

    Two things end a block: a blank line that is not inside a fenced code block,
    and a fence that has opened but not closed. A blank line inside ``` does not
    end the code, and a code block must not stay in the live region: its height
    changes on every line appended, so redrawing it repaints the whole block each
    frame. Printing it as it arrives costs one unstyled flash and stops the
    repaint.
    """
    lines = text.splitlines(keepends=True)
    fence = False
    last_end = 0
    offset = 0
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence and not line.strip() and index + 1 < len(lines):
            last_end = offset + len(line)
        offset += len(line)
    if fence:
        # An open fence must not settle: every appended line re-heights the
        # rendered code block, so settling it mid-stream would reprint it on
        # the next delta. It waits in the pending tail until it closes.
        return text[:last_end], text[last_end:]
    return text[:last_end], text[last_end:]


def format_status_line(
    *,
    active: bool,
    tokens_per_second: float | None = None,
    context_fraction: float | None = None,
    cached_fraction: float | None = None,
    reasoning_tokens: int | None = None,
    trace_id: str | None = None,
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


def console_event_handler(event: Event) -> None:
    match event.type:
        case "agent_step":
            step = event.data.get("step")
            max_steps = event.data.get("max_steps")

            console.print(f"[dim]step {step}/{max_steps}[/dim]")

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
            if not event.data.get("persisted", True):
                error = event.data.get("persistence_error")
                console.print(
                    f"[yellow]trace unavailable: persistence failed ({error})"
                    f"{_usage_suffix(event)}[/yellow]"
                )


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

    def __init__(self) -> None:
        self.final_text_visible = False
        self._reasoning: list[str] = []
        self._reasoning_open = False
        self._answer: list[str] = []
        self._settled_blocks = 0
        self._settled_chars = 0
        self._live: Live | None = None
        # Counters for the status line. Updated on every delta; the line itself
        # is repainted on a timer, so a burst of deltas costs one repaint.
        self._turn_started: float | None = None
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_tokens = 0
        self._reasoning_tokens: int | None = None
        self._trace_id: str | None = None
        self._context_used: int | None = None
        self._context_limit: int | None = None
        self._status_painted = 0.0
        self._text_seen = False
        self._active = False
        self._completed_text_step: int | None = None

    def reset(self) -> None:
        self.final_text_visible = False
        self._completed_text_step = None
        self._reasoning = []
        self._reasoning_open = False
        self._answer = []
        self._settled_blocks = 0
        self._settled_chars = 0
        self._status_painted = 0.0
        self._text_seen = False
        self._active = False

    def observe(self, event: Event) -> bool:
        """Return True only for handled streaming events; legacy output stays put."""
        if event.type == "model_stream_start":
            self._completed_text_step = None
            self._text_seen = False
            self._reasoning = []
            self._reasoning_open = False
            self._answer = []
            self._settled_blocks = 0
            self._settled_chars = 0
            self._turn_started = perf_counter()
            self._input_tokens = 0
            self._output_tokens = 0
            self._cached_tokens = 0
            self._reasoning_tokens = None
            self._trace_id = None
            self._active = True
            return True
        if event.type in ("model_text_delta", "model_reasoning_delta"):
            if event.type == "model_text_delta":
                self._text_seen = True
                self._answer.append(_plain_text(event))
                self._render_answer_progress()
                return True
            self._reasoning.append(_plain_text(event))
            self._render_reasoning()
            return True
        if event.type == "model_stream_end":
            status = event.data["status"]
            if status == "completed":
                self._active = False
                self._close_reasoning()
                if self._text_seen:
                    self._completed_text_step = event.data.get("step")
                    self.final_text_visible = True
                    self._render_answer()
            else:
                self.close_incomplete(abandoned=status == "abandoned")
            return True
        if event.type == "trace_finish":
            # The turn's totals arrive once, at the end; the status line already
            # shows live progress from the counters it accumulated per delta.
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
            self._refresh_status()
            return True
        if event.type == "agent_finish":
            self.final_text_visible = (
                self._completed_text_step == event.data.get("step")
                and self._completed_text_step is not None
            )
        return False

    def _refresh_status(self) -> None:
        """Repaint the live region if it is showing.

        Called on the events that change what the status line says, not per
        delta: a token stream can deliver dozens of updates a second and each
        repaint costs a full region redraw.
        """
        if self._live is None:
            return
        now = perf_counter()
        if now - self._status_painted < STATUS_INTERVAL_SECONDS:
            return
        self._status_painted = now
        self._live.update(self._live_renderable())

    def _render_reasoning(self) -> None:
        """Show the trailing reasoning lines; live where a live region is safe.

        In a terminal the block rides the shared live region, whose row count
        `rich` computes from the actual render, so a repaint erases exactly
        what it drew and interleaved tool output cannot shift the target. Off
        terminal there is no cursor to move, so each delta's trailing lines
        are appended once instead: a log shows the reasoning in order without
        cursor artifacts.
        """
        if supports_live_display():
            self._reasoning_open = True
            if self._live is None:
                self._start_live()
            else:
                self._refresh_status()
            return
        for line in self._visible_reasoning():
            console.print(f"[dim]{THINKING_PREFIX}{line}[/dim]", highlight=False)

    def _visible_reasoning(self) -> list[str]:
        """The last few complete lines of reasoning, wrapped to the terminal."""
        text = "".join(self._reasoning)
        lines: list[str] = []
        width = max(20, console.width - len(THINKING_PREFIX))
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            while len(stripped) > width:
                lines.append(stripped[:width])
                stripped = stripped[width:]
            lines.append(stripped)
        return lines[-MAX_THINKING_LINES:]

    def _close_reasoning(self) -> None:
        if not self._reasoning_open:
            return
        # The live copy is transient; reprint the final trailing lines once so
        # they stay in the scrollback after the region clears.
        lines = self._visible_reasoning()
        self._close_live()
        for line in lines:
            console.print(f"[dim]{THINKING_PREFIX}{line}[/dim]", highlight=False)
        self._reasoning_open = False
        console.print()

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
            if not self._settled_blocks:
                console.print("[bold]Cairn>[/bold]")
            console.print(Markdown(fresh))
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

        transient: each block is redrawn here, then printed once for good when
        it settles, so the live copy must not survive the stop.
        """
        self._live = Live(
            self._live_renderable(),
            console=console,
            refresh_per_second=8,
            vertical_overflow="visible",
            transient=True,
        )
        self._live.start()
        self._status_painted = perf_counter()

    def _live_renderable(self) -> Group:
        """The block still being written, the status line, and trailing reasoning.

        One renderable, not two live regions: `rich` only paints the top of its
        live stack, so a second Live for the status line would never appear.
        """
        body = Markdown("".join(self._answer)[self._settled_chars :])
        return Group(body, Text(""), self._status_line(), self._reasoning_renderable())

    def _reasoning_renderable(self) -> Text:
        """The trailing reasoning lines as one dim block, empty when not streaming."""
        return Text(
            "\n".join(f"{THINKING_PREFIX}{line}" for line in self._visible_reasoning()),
            style="dim",
        )

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
            if not self._settled_blocks:
                console.print("[bold]Cairn>[/bold]")
            console.print(Markdown(pending))
            return
        self._close_live()
        if not self._settled_blocks:
            console.print("[bold]Cairn>[/bold]")
            console.print(Markdown(text))

    def close_incomplete(self, *, abandoned: bool = False) -> None:
        """Abandon an attempt: drop partial output and say so.

        An unfinished markdown block is discarded rather than printed, because a
        half-written table or code fence reads as corruption once it stops
        changing.
        """
        self._close_live()
        # Discard the attempt completely: a retry after this must start from an
        # empty answer, not believe some of it is already on screen.
        self._answer = []
        self._reasoning = []
        self._settled_blocks = 0
        self._settled_chars = 0
        if self._reasoning_open:
            console.print()
            self._reasoning_open = False
        if self._active:
            self._active = False
            console.print(
                "[attempt abandoned; incomplete output discarded]"
                if abandoned
                else "[response incomplete; incomplete output discarded]",
                style="yellow",
                markup=False,
            )
        self._completed_text_step = None


def console_permission_prompt(request: PermissionRequest) -> PermissionChoice:
    console.print("\n[bold yellow]Permission required[/bold yellow]\n")
    console.print(f"Capability: {request.capability.value}", markup=False)

    command = request.tool_call.arguments.get("command")
    if isinstance(command, str):
        console.print(f"Command: {command}", markup=False)

    console.print(f"Reason: {request.justification}\n", markup=False)
    console.print(
        "[1] Allow once\n"
        f"[2] Allow {request.capability.value} for this session\n"
        "[3] Deny",
        markup=False,
    )
    choice = Prompt.ask("Choice", choices=["1", "2", "3"], default="3", console=console)
    return {
        "1": PermissionChoice.ALLOW_ONCE,
        "2": PermissionChoice.ALLOW_SESSION,
        "3": PermissionChoice.DENY,
    }[choice]


def print_runtime_error(exc: Exception) -> None:
    console.print(f"✗ {type(exc).__name__}: {exc}", style="bold red", markup=False)
