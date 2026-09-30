from importlib.resources import files

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Confirm
from rich.text import Text

from cairn.core.events import Event
from cairn.core.models import ToolCall
from cairn.core.permissions import (
    PermissionDecision,
    PermissionResult,
    check_permission,
)

console = Console()


def print_assistant_response(content: str) -> None:
    console.print("[bold]Cairn>[/bold]")
    console.print(Markdown(content))
    console.print()


def print_banner() -> None:
    console.print(files("cairn.resources").joinpath("banner.txt").read_text("utf-8"))


def _render_tool_call(event: Event) -> None:
    raw_tool = event.data.get("tool")
    tool = raw_tool if isinstance(raw_tool, str) else "tool"
    raw_arguments = event.data.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}

    if tool == "read_file":
        path = arguments.get("path")
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line")
        path_suffix = f" {path}" if isinstance(path, str) else ""
        if end is None or not path_suffix:
            summary = f"→ read_file{path_suffix}"
        else:
            summary = f"→ read_file{path_suffix} lines {start}-{end}"
    elif tool == "edit_file":
        path = arguments.get("path")
        path_suffix = f" {path}" if isinstance(path, str) else ""
        summary = f"→ edit_file{path_suffix}"
    elif tool == "bash":
        command = arguments.get("command")
        command_suffix = f": {command}" if isinstance(command, str) else ""
        summary = f"→ bash{command_suffix}"
    else:
        summary = f"→ {tool}"

    console.print(Text(f"\n{summary}", style="bold cyan"))


def _render_tool_result(event: Event) -> None:
    raw_tool = event.data.get("tool")
    tool = raw_tool if isinstance(raw_tool, str) else "tool"
    exit_code = event.data["exit_code"]
    raw_stdout = event.data.get("stdout")
    stdout = raw_stdout if isinstance(raw_stdout, str) else ""
    raw_stderr = event.data.get("stderr")
    stderr = raw_stderr if isinstance(raw_stderr, str) else ""

    if exit_code != 0:
        console.print(f"✗ exit {exit_code}", style="bold red", markup=False)
        diagnostic = stderr or stdout
        if diagnostic:
            console.print(diagnostic.rstrip(), style="yellow", markup=False)
        return

    if tool == "read_file":
        summary = "✓ read_file"
    elif tool == "edit_file":
        detail = stdout.strip()
        summary = f"✓ {detail}" if detail else "✓ edit_file"
    elif tool == "bash":
        summary = f"✓ exit {exit_code}"
    else:
        summary = f"✓ {tool}"

    console.print(summary, style="green", markup=False)


def _usage_suffix(event: Event) -> str:
    usage = event.data.get("usage")
    if not isinstance(usage, dict):
        return ""

    raw_input_tokens = usage.get("input_tokens")
    raw_output_tokens = usage.get("output_tokens")
    input_tokens = (
        str(raw_input_tokens)
        if isinstance(raw_input_tokens, int) and not isinstance(raw_input_tokens, bool)
        else "?"
    )
    output_tokens = (
        str(raw_output_tokens)
        if isinstance(raw_output_tokens, int)
        and not isinstance(raw_output_tokens, bool)
        else "?"
    )
    return f" · tokens: input {input_tokens}, output {output_tokens}"


def console_event_handler(event: Event) -> None:
    match event.type:
        case "agent_step":
            step = event.data.get("step")
            max_steps = event.data.get("max_steps")

            console.print(f"[dim]step {step}/{max_steps}[/dim]")

        case "tool_call":
            _render_tool_call(event)

        case "tool_result":
            _render_tool_result(event)

        case "tool_error":
            tool = event.data["tool"]
            error = event.data["error"]

            console.print(f"✗ {tool}: {error}", style="bold red", markup=False)

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


def console_permission_handler(tool_call: ToolCall) -> PermissionResult:

    decision = check_permission(tool_call)

    if decision == PermissionDecision.ALLOW:
        return PermissionResult(
            policy_decision=decision,
            allowed=True,
        )

    if decision == PermissionDecision.DENY:
        return PermissionResult(
            policy_decision=decision,
            allowed=False,
        )

    console.print("\n[bold yellow]Permission required[/bold yellow]")
    tool_label = Text("Tool:", style="bold")
    tool_label.append(f" {tool_call.name}")
    console.print(tool_label)

    if tool_call.name == "bash":
        command = tool_call.arguments.get("command")
        command_label = Text("Command:", style="bold")
        command_label.append(f" {command}")
        console.print(command_label)
    else:
        console.print(f"Arguments: {tool_call.arguments}", markup=False)

    allowed = Confirm.ask(
        "Allow this action?",
        default=False,
    )

    return PermissionResult(
        policy_decision=PermissionDecision.ASK,
        allowed=allowed,
        prompted=True,
    )


def print_runtime_error(exc: Exception) -> None:
    console.print(f"✗ {type(exc).__name__}: {exc}", style="bold red", markup=False)
