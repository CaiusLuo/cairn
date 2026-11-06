"""Host composition and thin CLI commands for the shared runtime."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from cairn.infra.runtime_client import RuntimeClient
from cairn.infra.runtime_client import read_runtime_token as read_token

app = typer.Typer(
    pretty_exceptions_show_locals=False, help="Use one shared Cairn Agent runtime."
)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def serve(ctx: typer.Context) -> None:
    """Start the Starlette host, forwarding its native options and help."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "cairn.web.runtime_server", *ctx.args], check=False
    )
    raise typer.Exit(result.returncode)


def client(url: str, token_file: Path) -> RuntimeClient:
    return RuntimeClient(url, read_token(token_file))


def output(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command()
def submit(
    prompt: str,
    token_file: Annotated[Path, typer.Option()],
    url: str = "http://127.0.0.1:8777",
    request_id: str | None = None,
) -> None:
    output(asyncio.run(client(url, token_file).submit(prompt, "cli", request_id)))


@app.command()
def inspect(
    task_id: str,
    token_file: Annotated[Path, typer.Option()],
    url: str = "http://127.0.0.1:8777",
) -> None:
    output(asyncio.run(client(url, token_file).inspect(task_id)))


@app.command("list")
def list_tasks(
    token_file: Annotated[Path, typer.Option()], url: str = "http://127.0.0.1:8777"
) -> None:
    output(asyncio.run(client(url, token_file).request("GET", "tasks")))


@app.command("continue")
def continue_task(
    task_id: str,
    token_file: Annotated[Path, typer.Option()],
    url: str = "http://127.0.0.1:8777",
) -> None:
    output(asyncio.run(client(url, token_file).control(task_id, "continue")))


@app.command()
def cancel(
    task_id: str,
    token_file: Annotated[Path, typer.Option()],
    url: str = "http://127.0.0.1:8777",
) -> None:
    output(asyncio.run(client(url, token_file).control(task_id, "cancel")))


@app.command()
def follow(
    task_id: str,
    token_file: Annotated[Path, typer.Option()],
    url: str = "http://127.0.0.1:8777",
    after: int = 0,
) -> None:
    RuntimeClient._id(task_id)

    async def run() -> None:
        cursor = after
        api = client(url, token_file)
        while True:
            response = await api.request(
                "GET", f"tasks/{task_id}/events?after={cursor}&wait=1"
            )
            for event in response["events"]:
                output(event)
            cursor = response["cursor"]
            if (await api.inspect(task_id))["status"] not in {"queued", "running"}:
                return

    asyncio.run(run())


@app.command()
def recovery(
    task_id: str,
    token_file: Annotated[Path, typer.Option()],
    url: str = "http://127.0.0.1:8777",
) -> None:
    """Inspect surviving effects; save this JSON for explicit reconciliation."""
    RuntimeClient._id(task_id)
    output(
        asyncio.run(client(url, token_file).request("GET", f"tasks/{task_id}/recovery"))
    )


@app.command()
def reconcile(
    task_id: str,
    report_file: Annotated[Path, typer.Option()],
    note: Annotated[str, typer.Option()],
    token_file: Annotated[Path, typer.Option()],
    effects_reviewed: bool = False,
    url: str = "http://127.0.0.1:8777",
) -> None:
    """Acknowledge inspected effects without replaying; continue is a separate action."""
    RuntimeClient._id(task_id)
    if not effects_reviewed:
        raise typer.BadParameter("Inspect effects and pass --effects-reviewed")
    report = json.loads(report_file.read_text())
    if report.get("task_id") != task_id:
        raise typer.BadParameter("Recovery report belongs to another task")
    payload = {key: report[key] for key in ("expected_sequence", "revision", "action")}
    payload.update({"reviewed_effects": True, "note": note})
    output(
        asyncio.run(
            client(url, token_file).request(
                "POST", f"tasks/{task_id}/reconcile", payload
            )
        )
    )


@app.command()
def history(
    task_id: str,
    token_file: Annotated[Path, typer.Option()],
    url: str = "http://127.0.0.1:8777",
) -> None:
    """Read the shared committed conversation without executing anything."""
    RuntimeClient._id(task_id)
    output(
        asyncio.run(client(url, token_file).request("GET", f"tasks/{task_id}/history"))
    )
