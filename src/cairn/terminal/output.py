import sys
from importlib.resources import files

from rich.console import Console
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
    """Per-CLI transient display state; it never advances an Agent lifecycle."""

    def __init__(self) -> None:
        self.final_text_visible = False
        self._open = False
        self._channel: str | None = None
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
            self._channel = None
            self._active = True
            return True
        if event.type in ("model_text_delta", "model_reasoning_delta"):
            if not self._open or self._channel != event.type:
                if self._open:
                    console.print()
                label = (
                    "[dim]thinking…[/dim]"
                    if event.type == "model_reasoning_delta"
                    else ""
                )
                console.print(f"[bold]Cairn>[/bold] {label}".rstrip())
                self._open = True
                self._channel = event.type
            if event.type == "model_text_delta":
                self._text_seen = True
            text = str(event.data["text"])
            # Rich markup and C0/C1 terminal controls have no authority. Preserve
            # newline/tab and printable Unicode, including multibyte fragments.
            plain = "".join(
                char
                for char in text
                if char in "\n\t" or not (ord(char) < 32 or 127 <= ord(char) <= 159)
            )
            console.print(
                plain,
                end="",
                markup=False,
                highlight=False,
                emoji=False,
                soft_wrap=True,
            )
            return True
        if event.type == "model_stream_end":
            status = event.data["status"]
            if status == "completed":
                self._active = False
                if self._text_seen:
                    self._completed_text_step = event.data.get("step")
                if self._open:
                    console.print()
                    self._open = False
            else:
                self.close_incomplete(abandoned=status == "abandoned")
            return True
        if event.type == "agent_finish":
            self.final_text_visible = (
                self._completed_text_step == event.data.get("step")
                and self._completed_text_step is not None
            )
        return False

    def close_incomplete(self, *, abandoned: bool = False) -> None:
        if self._open:
            console.print()
            self._open = False
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
