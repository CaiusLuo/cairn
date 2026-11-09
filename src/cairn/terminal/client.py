"""Independent terminal client; every execution request goes to Server."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import httpx
import typer

from cairn.infra.runtime_client import (
    RuntimeClient,
    read_runtime_token,
)
from cairn.infra.server_config import load_server_config
from cairn.terminal.runtime import app as runtime_commands

app = typer.Typer(
    invoke_without_command=True,
    pretty_exceptions_show_locals=False,
    help="Connect the interactive CLI to Cairn Server from this directory.",
)
app.add_typer(runtime_commands, name="runtime")


@app.callback()
def root(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    runtime_url: Annotated[
        str | None, typer.Option("--runtime-url", envvar="CAIRN_RUNTIME_URL")
    ] = None,
    runtime_token_file: Annotated[
        Path | None,
        typer.Option("--runtime-token-file", envvar="CAIRN_RUNTIME_TOKEN_FILE"),
    ] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
) -> None:
    from cairn.terminal.runtime_chat import main as shared_main

    async def connect() -> None:
        api = RuntimeClient(
            selected_url, read_runtime_token(selected_token), workspace=Path.cwd()
        )
        await api.validate_workspace()
        await shared_main(api, task_id=task_id)

    try:
        settings = load_server_config(config)
        selected_url = runtime_url or settings.url
        selected_token = (
            runtime_token_file or settings.data_root / "runtime/runtime.token"
        )
        if not selected_token.is_absolute():
            raise ValueError("Runtime token path must be absolute")
        ctx.obj = {"url": selected_url, "token_file": selected_token}
        if ctx.invoked_subcommand is not None:
            return
        asyncio.run(connect())
    except httpx.HTTPStatusError as error:
        typer.echo(
            f"Server refused connection (HTTP {error.response.status_code}); check the Runtime token and workspace.",
            err=True,
        )
        raise typer.Exit(1) from None
    except httpx.TransportError:
        typer.echo(
            f"Cannot connect to Cairn Server at {selected_url}; start cairn or configure --runtime-url. No local Agent was started.",
            err=True,
        )
        raise typer.Exit(1) from None
    except (OSError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from None
