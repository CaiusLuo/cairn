from pathlib import Path

import typer
from rich.console import Console

from cairn.observability.reader import JsonlTraceReader
from cairn.trace_ui import print_trace

trace_app = typer.Typer(
    help="Inspect Cairn execution traces.",
)

console = Console()


@trace_app.command("show")
def show_trace(trace_id: str) -> None:
    reader = JsonlTraceReader(Path(".cairn/traces"))

    try:
        spans = reader.read(trace_id)
    except (FileNotFoundError, ValueError) as exc:
        console.print(str(exc), style="red")
        raise typer.Exit(code=1) from exc

    print_trace(spans)
