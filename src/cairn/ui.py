from importlib.resources import files

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Confirm

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


def console_event_handler(event: Event) -> None:
    match event.type:
        case "agent_step":
            step = event.data.get("step")
            max_steps = event.data.get("max_steps")

            console.print(f"[dim]step {step}/{max_steps}[/dim]")

        case "tool_call":
            tool = event.data.get("tool")
            arguments = event.data.get("arguments")

            console.print(f"\n[bold cyan]→ {tool}[/bold cyan]")
            console.print(f"[dim]{arguments}[/dim]")

        case "tool_result":
            exit_code = event.data["exit_code"]
            stdout = event.data["stdout"]
            stderr = event.data["stderr"]

            console.print(f"[green]← exit {exit_code}[/green]")

            if stdout:
                console.print(stdout.rstrip())

            if stderr:
                console.print(
                    stderr.rstrip(),
                    style="yellow",
                )

        case "tool_error":
            tool = event.data["tool"]
            error = event.data["error"]

            console.print(f"[bold red]✗ {tool}: {error}[/bold red]")

        case "agent_finish":
            console.print("[dim]✓ done[/dim]")

        case "agent_step_limit":
            max_steps = event.data.get("max_steps")

            console.print(
                f"[bold red]Agent stopped after {max_steps} steps.[/bold red]"
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
    console.print(f"[bold]Tool:[/bold] {tool_call.name}")

    if tool_call.name == "bash":
        command = tool_call.arguments.get("command")
        console.print(f"[bold]Command:[/bold] {command}")
    else:
        console.print(f"[bold]Arguments:[/bold] {tool_call.arguments}")

    allowed = Confirm.ask(
        "Allow this action?",
        default=False,
    )

    return PermissionResult(
        policy_decision=PermissionDecision.ASK,
        allowed=allowed,
        prompted=True,
    )
