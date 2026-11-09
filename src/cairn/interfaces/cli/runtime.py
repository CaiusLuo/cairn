"""Host composition and thin CLI commands for the shared runtime."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from cairn.server.client import RuntimeClient
from cairn.server.client import read_runtime_token as read_token
from cairn.server.config import load_server_config

app = typer.Typer(
    pretty_exceptions_show_locals=False, help="Use one shared Cairn Agent runtime."
)


def client(
    ctx: typer.Context, url: str | None, token_file: Path | None
) -> RuntimeClient:
    defaults = ctx.obj
    if not isinstance(defaults, dict):
        settings = load_server_config()
        defaults = {
            "url": settings.url,
            "token_file": settings.data_root / "runtime/runtime.token",
        }
    path = token_file or defaults["token_file"]
    if not path.is_absolute():
        raise typer.BadParameter("Runtime token path must be absolute")
    return RuntimeClient(url or defaults["url"], read_token(path))


def output(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command()
def submit(
    ctx: typer.Context,
    prompt: str,
    token_file: Annotated[Path | None, typer.Option()] = None,
    url: str | None = None,
    request_id: str | None = None,
) -> None:
    output(asyncio.run(client(ctx, url, token_file).submit(prompt, "cli", request_id)))


@app.command()
def inspect(
    ctx: typer.Context,
    task_id: str,
    token_file: Annotated[Path | None, typer.Option()] = None,
    url: str | None = None,
) -> None:
    output(asyncio.run(client(ctx, url, token_file).inspect(task_id)))


@app.command("list")
def list_tasks(
    ctx: typer.Context,
    token_file: Annotated[Path | None, typer.Option()] = None,
    url: str | None = None,
) -> None:
    output(asyncio.run(client(ctx, url, token_file).request("GET", "tasks")))


@app.command("continue")
def continue_task(
    ctx: typer.Context,
    task_id: str,
    token_file: Annotated[Path | None, typer.Option()] = None,
    url: str | None = None,
) -> None:
    output(asyncio.run(client(ctx, url, token_file).control(task_id, "continue")))


@app.command()
def cancel(
    ctx: typer.Context,
    task_id: str,
    token_file: Annotated[Path | None, typer.Option()] = None,
    url: str | None = None,
) -> None:
    output(asyncio.run(client(ctx, url, token_file).control(task_id, "cancel")))


@app.command()
def follow(
    ctx: typer.Context,
    task_id: str,
    token_file: Annotated[Path | None, typer.Option()] = None,
    url: str | None = None,
    after: int = 0,
) -> None:
    RuntimeClient._id(task_id)

    async def run() -> None:
        cursor = after
        api = client(ctx, url, token_file)
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
    ctx: typer.Context,
    task_id: str,
    token_file: Annotated[Path | None, typer.Option()] = None,
    url: str | None = None,
) -> None:
    """Inspect surviving effects; save this JSON for explicit reconciliation."""
    RuntimeClient._id(task_id)
    output(
        asyncio.run(
            client(ctx, url, token_file).request("GET", f"tasks/{task_id}/recovery")
        )
    )


@app.command()
def reconcile(
    ctx: typer.Context,
    task_id: str,
    report_file: Annotated[Path, typer.Option()],
    note: Annotated[str, typer.Option()],
    token_file: Annotated[Path | None, typer.Option()] = None,
    effects_reviewed: bool = False,
    url: str | None = None,
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
            client(ctx, url, token_file).request(
                "POST", f"tasks/{task_id}/reconcile", payload
            )
        )
    )


@app.command()
def history(
    ctx: typer.Context,
    task_id: str,
    token_file: Annotated[Path | None, typer.Option()] = None,
    url: str | None = None,
) -> None:
    """Read the shared committed conversation without executing anything."""
    RuntimeClient._id(task_id)
    output(
        asyncio.run(
            client(ctx, url, token_file).request("GET", f"tasks/{task_id}/history")
        )
    )
