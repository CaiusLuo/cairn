"""Independent terminal client and explicit migration commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import httpx
import typer

from cairn.infra.runtime_client import (
    RuntimeClient,
    default_runtime_state_dir,
    read_runtime_token,
)
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
    runtime_url: Annotated[
        str, typer.Option("--runtime-url", envvar="CAIRN_RUNTIME_URL")
    ] = "http://127.0.0.1:8777",
    runtime_token_file: Annotated[
        Path | None,
        typer.Option("--runtime-token-file", envvar="CAIRN_RUNTIME_TOKEN_FILE"),
    ] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    from cairn.terminal.runtime_chat import main as shared_main

    async def connect() -> None:
        api = RuntimeClient(
            runtime_url,
            read_runtime_token(
                runtime_token_file or default_runtime_state_dir() / "runtime.token"
            ),
            workspace=Path.cwd(),
        )
        await api.validate_workspace()
        await shared_main(api, task_id=task_id)

    try:
        asyncio.run(connect())
    except httpx.HTTPStatusError as error:
        typer.echo(
            f"Server refused connection (HTTP {error.response.status_code}); check the Runtime token and workspace.",
            err=True,
        )
        raise typer.Exit(1) from None
    except httpx.TransportError:
        typer.echo(
            f"Cannot connect to Cairn Server at {runtime_url}; start cairn or configure --runtime-url. No local Agent was started.",
            err=True,
        )
        raise typer.Exit(1) from None
    except (OSError, ValueError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from None


def _legacy(command: str, ctx: typer.Context) -> None:
    from cairn.terminal.cli import app as legacy_app

    legacy_app(
        args=([command] if command else []) + ctx.args,
        prog_name="cairn-cli" + (" local" if not command else ""),
        standalone_mode=False,
    )


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def local(ctx: typer.Context) -> None:
    """Explicit legacy local Agent (not shared server work)."""
    _legacy("", ctx)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def session(ctx: typer.Context) -> None:
    """Legacy local durable Session commands."""
    _legacy("session", ctx)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def task(ctx: typer.Context) -> None:
    """Legacy headless task commands; use runtime submit for shared tasks."""
    _legacy("task", ctx)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def wechat(ctx: typer.Context) -> None:
    """iLink login, shared bridge and standalone migration commands."""
    _legacy("wechat", ctx)
