import sys
from importlib.resources import files

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.prompt import Confirm, Prompt

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
# The marker that sets reasoning apart from the answer it precedes.
THINKING_PREFIX = "⋮ "


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
    """Split Markdown into finished blocks and the trailing partial one.

    A block ends at a blank line that is not inside a fenced code block, because
    a blank line inside ``` does not end the code. Separating them lets the caller
    print every finished block once and redraw only the tail still being written.
    """
    fence = False
    last_end = 0
    index = 0
    lines = text.splitlines(keepends=True)
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence and not line.strip() and index + 1 < len(lines):
            last_end = sum(len(item) for item in lines[: index + 1])
        index += 1
    return text[:last_end], text[last_end:]


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
            trace_id = event.data["trace_id"]
            status = event.data["status"]
            persisted = event.data.get("persisted", True)
            usage_suffix = _usage_suffix(event)

            if persisted:
                console.print(f"[dim]trace: {trace_id} ({status}){usage_suffix}[/dim]")
            else:
                error = event.data.get("persistence_error")
                console.print(
                    f"[yellow]trace unavailable: persistence failed ({error})"
                    f"{usage_suffix}[/yellow]"
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
    """

    def __init__(self) -> None:
        self.final_text_visible = False
        self._reasoning: list[str] = []
        self._reasoning_open = False
        self._answer: list[str] = []
        self._settled_blocks = 0
        self._settled_chars = 0
        self._live: Live | None = None
        self._text_seen = False
        self._active = False
        self._completed_text_step: int | None = None

    def reset(self) -> None:
        self.final_text_visible = False
        self._completed_text_step = None

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
            self._active = True
            return True
        if event.type in ("model_text_delta", "model_reasoning_delta"):
            if event.type == "model_text_delta":
                self._text_seen = True
                self._answer.append(_plain_text(event))
                self._render_answer_progress()
                return True
            self._reasoning.append(_plain_text(event))
            self._print_reasoning()
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
        if event.type == "agent_finish":
            self.final_text_visible = (
                self._completed_text_step == event.data.get("step")
                and self._completed_text_step is not None
            )
        return False

    def _print_reasoning(self) -> None:
        """Redraw the trailing reasoning lines in place.

        The block is rewritten on every delta instead of appended to, so the
        screen shows the current thread rather than the whole transcript. A
        terminal cursor move expresses that without a full- screen renderer.
        """
        if self._reasoning_open:
            console.file.write(f"\x1b[{self._reasoning_height()}A")
            console.file.write("\x1b[J")
        else:
            self._reasoning_open = True
        for line in self._visible_reasoning():
            console.print(f"[dim]{THINKING_PREFIX}{line}[/dim]", highlight=False)
        console.file.flush()

    def _reasoning_height(self) -> int:
        return len(self._visible_reasoning())

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
        # The last render already shows the final lines; only clear the cursor
        # bookkeeping so the next block starts below them.
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
        if self._live is None:
            # transient: the block is redrawn here, then printed once for good
            # when it settles, so the live copy must not survive the stop.
            self._live = Live(
                Markdown(pending),
                console=console,
                refresh_per_second=8,
                vertical_overflow="visible",
                transient=True,
            )
            self._live.start()
        else:
            self._live.update(Markdown(pending))

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
