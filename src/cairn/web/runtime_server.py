"""Starlette runtime host; CLI clients do not import the browser adapter."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn

from cairn.core.budget import RunBudget
from cairn.infra.config.admission import ProviderApproval, load_approved_provider
from cairn.infra.config.runtime_budget import load_task_budget
from cairn.infra.runtime_catalog import RuntimeCatalogue
from cairn.infra.runtime_client import default_runtime_state_dir
from cairn.infra.runtime_client import read_runtime_token as read_token
from cairn.infra.task_board.store import TaskBoardStore
from cairn.runtime.offline_model import OfflineDevelopmentModel
from cairn.runtime.provider_runtime import ProviderRuntime, build_provider_runtime
from cairn.runtime.service_execution import ServiceExecution
from cairn.runtime.task_board.service import TaskBoardService
from cairn.runtime.task_service import TaskService
from cairn.web.app import create_app
from cairn.web.cli import _default_web_root
from cairn.web.config import WebConfig

app = typer.Typer(
    pretty_exceptions_show_locals=False,
    help="Run Cairn Server: shared Agent, task queue, HTTP API and Web.",
)


def runtime_app(
    workspace: Path,
    state: Path,
    worktrees: Path,
    *,
    budget: RunBudget,
    token: str,
    web_root: Path | None = None,
    port: int = 8777,
    provider_runtime: ProviderRuntime | None = None,
    secret_env_keys: frozenset[str] = frozenset(),
    sensitive_values: tuple[str, ...] = (),
    allow_shell: bool = False,
    allow_network: bool = False,
) -> Any:
    catalogue = RuntimeCatalogue(
        state,
        forbidden_roots=(workspace, worktrees),
        sensitive_values=(token, *sensitive_values),
    )
    try:
        execution = ServiceExecution(
            workspace,
            state,
            worktrees,
            OfflineDevelopmentModel
            if provider_runtime is None
            else lambda: provider_runtime.llm,
            budget=budget,
            provider_runtime=provider_runtime,
            secret_env_keys=secret_env_keys,
            sensitive_values=(token, *sensitive_values),
            allow_shell=allow_shell,
            allow_network=allow_network,
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
    service.recovery_reader = execution.recovery
    service.reconciler = execution.reconcile
    try:
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
    except BaseException:
        execution.close()
        catalogue.close()
        raise

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        try:
            _ = execution.store
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
    workspace: Annotated[Path, typer.Option()] = Path("."),
    state_dir: Annotated[Path | None, typer.Option()] = None,
    worktrees: Annotated[Path | None, typer.Option()] = None,
    offline: Annotated[bool, typer.Option("--offline")] = False,
    port: int = 8777,
    max_steps: int | None = None,
    budget_config: Path | None = None,
    provider_approval: Path | None = None,
    allow_shell: bool = False,
    allow_network: bool = False,
    web_root: Path | None = None,
) -> None:
    workspace = workspace.expanduser().resolve()
    state_dir = (state_dir or default_runtime_state_dir()).expanduser().absolute()
    worktrees = (worktrees or state_dir.parent / "worktrees").expanduser().resolve()
    if offline == (provider_approval is not None):
        raise typer.BadParameter("Choose --offline or --provider-approval FILE")
    for config_path in (budget_config, provider_approval):
        if config_path is not None and (
            config_path.is_symlink()
            or any(
                config_path.resolve().is_relative_to(root.resolve())
                for root in (workspace, worktrees)
            )
        ):
            raise typer.BadParameter(
                "Host policy files must be outside every Agent-writable root"
            )
    budget = load_task_budget(budget_config, max_steps=max_steps)
    profile = None
    names: frozenset[str] = frozenset()
    known_values: tuple[str, ...] = ()
    if provider_approval is not None:
        data = json.loads(provider_approval.read_text())
        fields = {"name", "endpoint", "group", "key_env", "model_ids"}
        if (
            not isinstance(data, dict)
            or set(data) != fields
            or not isinstance(data["model_ids"], list)
            or not all(isinstance(value, str) for value in data["model_ids"])
        ):
            raise typer.BadParameter("Invalid provider approval file")
        data["model_ids"] = tuple(data["model_ids"])
        approval = ProviderApproval(**data)
        try:
            approved = load_approved_provider(workspace.resolve(), approval)
            names, known_values = approved.secret_env_keys, approved.sensitive_values
            profile = build_provider_runtime(
                approved.provider,
                approved.api_key,
                approved.context_budget,
                group=approved.group.name,
            )
        except Exception as exc:
            raise typer.BadParameter(
                "Provider configuration rejected: " + type(exc).__name__
            ) from None
    state_dir = state_dir.absolute()
    if any(
        state_dir == p.resolve()
        or state_dir.is_relative_to(p.resolve())
        or p.resolve().is_relative_to(state_dir)
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
        budget=budget,
        token=token,
        web_root=web_root,
        port=port,
        provider_runtime=profile,
        secret_env_keys=names,
        sensitive_values=known_values,
        allow_shell=allow_shell,
        allow_network=allow_network,
    )
    uvicorn.run(web, host="127.0.0.1", port=port)


def main() -> None:
    try:
        app()
    except (OSError, ValueError, RuntimeError) as error:
        typer.echo(f"Server startup failed: {error}", err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
