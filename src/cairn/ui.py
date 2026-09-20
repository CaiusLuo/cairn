from importlib.resources import files

from rich.console import Console

from rich.markdown import Markdown

from cairn.core.events import Event

console = Console()

def print_assistant_response(content: str) -> None:
    console.print("[bold]Cairn>[/bold]")
    console.print(Markdown(content))
    console.print()

def print_banner() -> None:
    console.print(
        files("cairn.resources")
            .joinpath("banner.txt")
            .read_text('utf-8')
        )

def console_event_handler(event: Event) -> None:
    match event.type:
        case "agent_step":
            step = event.data.get("step")
            max_steps = event.data.get("max_steps")

            console.print(
                f"[dim]step {step}/{max_steps}[/dim]"
            )

        case "tool_call":
            tool = event.data.get("tool")
            arguments = event.data.get("arguments")

            console.print(
                f"\n[bold cyan]→ {tool}[/bold cyan]"
            )
            console.print(
                f"[dim]{arguments}[/dim]"
            )

        case "tool_result":
            exit_code = event.data["exit_code"]
            stdout = event.data["stdout"]
            stderr = event.data["stderr"]

            console.print(
                f"[green]← exit {exit_code}[/green]"
            )

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

            console.print(
                f"[bold red]✗ {tool}: {error}[/bold red]"
            )

        case "agent_finish":
            console.print(
                "[dim]✓ done[/dim]"
            )

        case "agent_step_limit":
            max_steps = event.data.get("max_steps")

            console.print(
                f"[bold red]"
                f"Agent stopped after {max_steps} steps."
                f"[/bold red]"
            )