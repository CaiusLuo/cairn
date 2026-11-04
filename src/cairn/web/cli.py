"""Local startup command for the read-only web surface."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from cairn.infra.task_board.store import default_task_board_dir
from cairn.web.app import create_app
from cairn.web.config import WebConfig, default_sessions_dir

DEFAULT_SESSIONS_DIR = default_sessions_dir()
DEFAULT_TASKS_DIR = default_task_board_dir()

app = typer.Typer(
    invoke_without_command=True,
    help="Serve the Cairn web surface on loopback.",
)


def _default_web_root() -> Path:
    # src/cairn/web/cli.py -> repository root -> web/dist
    return Path(__file__).resolve().parents[3] / "web" / "dist"


@app.command()
def serve(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            help="Explicit trusted workspace root for this process.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ],
    sessions_dir: Annotated[
        Path,
        typer.Option(
            "--sessions-dir",
            help="Read-only SessionStore directory.",
            resolve_path=True,
        ),
    ] = DEFAULT_SESSIONS_DIR,
    tasks_dir: Annotated[
        Path,
        typer.Option(
            "--tasks-dir",
            help="Read-only shared TaskBoard directory.",
            resolve_path=True,
        ),
    ] = DEFAULT_TASKS_DIR,
    host: Annotated[
        str, typer.Option("--host", help="Loopback host only.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8765,
    refresh_seconds: Annotated[
        float,
        typer.Option(
            "--refresh-seconds",
            help="Bounded WS refresh polling interval.",
        ),
    ] = 2.0,
    web_root: Annotated[
        Path | None,
        typer.Option(
            "--web-root",
            help="Built Vite directory; defaults to repository web/dist.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Serve the Sessions/Tasks pages plus the guarded task status move."""
    auth_passphrase = os.environ.get("CAIRN_WEB_PASSWORD", "").strip()
    if not auth_passphrase:
        typer.echo(
            "CAIRN_WEB_PASSWORD is required for the public web surface.", err=True
        )
        raise typer.Exit(2)
    config = WebConfig(
        workspace=workspace,
        sessions_dir=sessions_dir,
        tasks_dir=tasks_dir,
        host=host,
        port=port,
        refresh_seconds=refresh_seconds,
        web_root=web_root or _default_web_root(),
        auth_passphrase=auth_passphrase,
    )
    typer.echo(f"Serving read-only Cairn web surface at {config.origin_for()}")
    uvicorn.run(
        create_app(config), host=config.host, port=config.port, log_level="info"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
