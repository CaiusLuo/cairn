"""Starlette runtime host; CLI clients do not import the browser adapter."""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn

from cairn.core.budget import RunBudget
from cairn.infra.runtime_catalog import RuntimeCatalogue
from cairn.infra.runtime_client import read_runtime_token as read_token
from cairn.infra.task_board.store import TaskBoardStore
from cairn.runtime.offline_model import OfflineDevelopmentModel
from cairn.runtime.service_execution import ServiceExecution
from cairn.runtime.task_board.service import TaskBoardService
from cairn.runtime.task_service import TaskService
from cairn.web.app import create_app
from cairn.web.cli import _default_web_root
from cairn.web.config import WebConfig

app = typer.Typer(help="Host the experimental shared Cairn runtime.")


def runtime_app(
    workspace: Path,
    state: Path,
    worktrees: Path,
    *,
    budget: RunBudget,
    token: str,
    web_root: Path | None = None,
    port: int = 8777,
) -> Any:
    catalogue = RuntimeCatalogue(state, forbidden_roots=(workspace, worktrees))
    try:
        execution = ServiceExecution(
            workspace, state, worktrees, OfflineDevelopmentModel, budget=budget
        )
    except BaseException:
        catalogue.close()
        raise
    service = TaskService(
        catalogue,
        execution,
        board=TaskBoardService(TaskBoardStore(state / "board"), project=str(workspace)),
    )
    execution.service = service
    service.history_reader = execution.history
    web = create_app(
        WebConfig(
            workspace=workspace,
            sessions_dir=state / "sessions",
            tasks_dir=state / "board",
            port=port,
            auth_passphrase=token,
            web_root=web_root if web_root is not None else _default_web_root(),
        ),
        runtime=service,
        runtime_token=token,
    )

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        try:
            await service.start()
            yield
        finally:
            try:
                await service.close()
            finally:
                execution.close()

    web.router.lifespan_context = lifespan
    web.state.runtime = service
    web.state.execution = execution
    return web


@app.command()
def serve(
    workspace: Annotated[Path, typer.Option()],
    state_dir: Annotated[Path, typer.Option()],
    worktrees: Annotated[Path, typer.Option()],
    offline: Annotated[bool, typer.Option("--offline")] = False,
    port: int = 8777,
    max_steps: int = 500,
    web_root: Path | None = None,
) -> None:
    if not offline:
        raise typer.BadParameter(
            "Use --offline for the acceptance model; explicit provider binding is required for paid models"
        )
    state_dir = state_dir.absolute()
    if any(
        state_dir == p.resolve() or state_dir.is_relative_to(p.resolve())
        for p in (workspace, worktrees)
    ):
        raise typer.BadParameter("Runtime state must be outside every writable root")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    token_file = state_dir / "runtime.token"
    if not token_file.exists():
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
    token = read_token(token_file)
    web = runtime_app(
        workspace.resolve(),
        state_dir,
        worktrees.resolve(),
        budget=RunBudget(max_steps=max_steps),
        token=token,
        web_root=web_root,
        port=port,
    )
    uvicorn.run(web, host="127.0.0.1", port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
