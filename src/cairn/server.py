"""Starlette runtime host; CLI clients do not import the browser adapter."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
from starlette.responses import JSONResponse
from starlette.routing import Route

from cairn.core.budget import RunBudget
from cairn.infra.config.admission import ProviderApproval, load_approved_provider
from cairn.infra.config.server import (
    DEFAULT_SERVICES,
    ServerConfig,
    Services,
    load_server_config,
)
from cairn.infra.runtime_catalog import RuntimeCatalogue
from cairn.infra.runtime_client import read_runtime_token as read_token
from cairn.infra.task_board.store import TaskBoardStore
from cairn.infra.workspace.lease import WorkspaceLease
from cairn.runtime.lifecycle import await_owned
from cairn.runtime.offline_model import OfflineDevelopmentModel
from cairn.runtime.provider_runtime import ProviderRuntime, build_provider_runtime
from cairn.runtime.service_execution import ServiceExecution
from cairn.runtime.task_board.service import TaskBoardService
from cairn.runtime.task_service import TaskService
from cairn.server_controls import ServerControls
from cairn.web.app import create_app
from cairn.web.assets import default_web_root, require_web_assets
from cairn.web.config import WebConfig
from cairn.web.management import management_routes

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
    wechat_state: Path | None = None,
    agent_home: Path | None = None,
    schedule_state: Path | None = None,
    host: str = "127.0.0.1",
    services: Services = DEFAULT_SERVICES,
    protected_paths: tuple[Path, ...] = (),
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
                host=host,
                port=port,
                auth_passphrase=token,
                web_root=web_root if web_root is not None else default_web_root(),
            ),
            runtime=service,
            runtime_token=token,
        )
    except BaseException:
        execution.close()
        catalogue.close()
        raise

    try:
        controls = ServerControls(
            service=service,
            execution=execution,
            workspace=workspace,
            state=state,
            token=token,
            web=web,
            port=port,
            wechat_state=wechat_state or state / "wechat",
            agent_home=agent_home or state / "agent",
            schedule_state=schedule_state or state / "schedules",
            services=services,
        )
    except BaseException:
        execution.close()
        catalogue.close()
        raise
    execution.protected_paths += protected_paths
    web.router.routes[0:0] = management_routes(controls, token)
    web.state.controls = controls

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        try:
            _ = execution.store
            await service.start()
            await controls.start()
            yield
        finally:
            primary = sys.exception()

            async def cleanup() -> None:
                try:
                    await controls.close()
                finally:
                    try:
                        await service.close()
                    finally:
                        execution.close()

            owned = asyncio.create_task(cleanup())
            late = await await_owned(owned)
            owned.result()
            if late is not None and primary is None:
                raise late

    web.router.lifespan_context = lifespan
    web.state.runtime = service
    web.state.execution = execution
    return web


def validate_config(config: ServerConfig) -> Path:
    paths = config.paths
    if config.offline == (config.provider_approval is not None):
        raise ValueError(
            "Choose explicit offline=true or provider_approval; no fallback model"
        )
    for path in (config.config_file, config.provider_approval):
        if path is not None:
            paths.protect_policy(path)
            if not path.is_file():
                raise ValueError(f"Host configuration file is missing: {path}")
    return require_web_assets(config.web_root or default_web_root())


def _load_provider(
    config: ServerConfig,
) -> tuple[ProviderRuntime | None, frozenset[str], tuple[str, ...]]:
    profile = None
    names: frozenset[str] = frozenset()
    known_values: tuple[str, ...] = ()
    if config.provider_approval is not None:
        try:
            data = json.loads(config.provider_approval.read_text())
            expected = {"name", "endpoint", "group", "key_env", "model_ids"}
            if (
                not isinstance(data, dict)
                or set(data) != expected
                or not isinstance(data["model_ids"], list)
                or not data["model_ids"]
                or not all(isinstance(value, str) for value in data["model_ids"])
                or not all(
                    isinstance(data[key], str) for key in expected - {"model_ids"}
                )
            ):
                raise ValueError("Invalid provider approval file")
            data["model_ids"] = tuple(data["model_ids"])
            approved = load_approved_provider(
                config.paths.workspace, ProviderApproval(**data)
            )
            names, known_values = approved.secret_env_keys, approved.sensitive_values
            profile = build_provider_runtime(
                approved.provider,
                approved.api_key,
                approved.context_budget,
                group=approved.group.name,
            )
        except Exception as exc:
            raise ValueError(
                "Provider configuration rejected: " + type(exc).__name__
            ) from None
    return profile, names, known_values


def run_server(config: ServerConfig) -> None:
    web_root = validate_config(config)
    paths = config.paths
    profile, names, known_values = _load_provider(config)
    paths.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.data_root.chmod(0o700)
    lease = WorkspaceLease(paths.data_root / "server.lock")
    if (paths.data_root / "server.lock").is_symlink():
        raise ValueError("Server lock cannot be a symlink")
    if not lease.acquire():
        raise RuntimeError("Another Cairn Server owns this data_root")
    sock = socket.socket(socket.AF_INET6 if config.host == "::1" else socket.AF_INET)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((config.host, config.port))
        sock.listen(2048)
        paths.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths.runtime.chmod(0o700)
        if not paths.token.exists():
            fd = os.open(paths.token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(32))
        token = read_token(paths.token)
        web = runtime_app(
            paths.workspace,
            paths.runtime,
            paths.worktrees,
            budget=config.budget,
            token=token,
            web_root=web_root,
            host=config.host,
            port=config.port,
            provider_runtime=profile,
            secret_env_keys=names,
            sensitive_values=known_values,
            allow_shell=config.allow_shell,
            allow_network=config.allow_network,
            wechat_state=paths.wechat,
            agent_home=paths.agent,
            schedule_state=paths.schedules,
            services=config.services,
            protected_paths=tuple(
                p for p in (config.config_file, config.provider_approval) if p
            ),
        )
        identity = {
            "pid": os.getpid(),
            "data_root": str(paths.data_root),
            "workspace": str(paths.workspace),
            "worktrees_root": str(paths.worktrees),
            "url": config.url,
        }

        async def bootstrap(_request: Any) -> JSONResponse:
            return JSONResponse(identity)

        web.router.routes.insert(
            0, Route("/api/runtime/bootstrap", bootstrap, methods=["GET"])
        )
        server = uvicorn.Server(
            uvicorn.Config(
                web,
                host=config.host,
                port=config.port,
                log_level=config.logging.level,
                access_log=config.logging.access_log,
            )
        )
        server.run(sockets=[sock])
        if not server.started:
            raise RuntimeError(
                "Server lifespan startup failed; see the preceding error"
            )
    finally:
        sock.close()
        lease.release()


@app.callback(invoke_without_command=True)
def serve(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    workspace: Annotated[Path | None, typer.Option()] = None,
    data_root: Annotated[Path | None, typer.Option()] = None,
    worktrees_root: Annotated[Path | None, typer.Option("--worktrees-root")] = None,
    host: str | None = None,
    port: int | None = None,
    offline: Annotated[bool | None, typer.Option("--offline/--no-offline")] = None,
    provider_approval: Path | None = None,
    web_root: Path | None = None,
    allow_shell: Annotated[
        bool | None, typer.Option("--allow-shell/--no-allow-shell")
    ] = None,
    allow_network: Annotated[
        bool | None, typer.Option("--allow-network/--no-allow-network")
    ] = None,
    max_steps: int | None = None,
    max_seconds: float | None = None,
    max_tokens: int | None = None,
    max_cost: float | None = None,
    max_stagnant_steps: int | None = None,
    input_cost_per_million: float | None = None,
    output_cost_per_million: float | None = None,
) -> None:
    values = dict(locals())
    for key in ("ctx", "config"):
        values.pop(key)
    settings = load_server_config(config, overrides=values)
    ctx.obj = settings
    if ctx.invoked_subcommand is None:
        run_server(settings)


@app.command("check-config")
def check_config(ctx: typer.Context) -> None:
    """Validate bootstrap paths and assets without creating state or loading keys."""
    settings: ServerConfig = ctx.obj
    validate_config(settings)
    _load_provider(settings)
    typer.echo(f"Configuration OK: {settings.url}; data_root={settings.data_root}")


@app.command("check-running")
def check_running(
    ctx: typer.Context,
    pid: Annotated[int | None, typer.Option("--pid")] = None,
) -> None:
    """Refuse restart when this config cannot identify the running Server."""
    from cairn.infra.runtime_client import RuntimeClient

    settings: ServerConfig = ctx.obj
    paths = settings.paths
    expected = {
        "data_root": str(paths.data_root),
        "workspace": str(paths.workspace),
        "worktrees_root": str(paths.worktrees),
        "url": settings.url,
    }
    try:
        api = RuntimeClient(settings.url, read_token(paths.token))
        actual = asyncio.run(api.request("GET", "bootstrap"))
    except Exception:
        raise RuntimeError(
            "Cannot identify running Server with this configuration; inspect the old service and stop it explicitly before changing data_root/port"
        ) from None
    if not isinstance(actual, dict) or (pid is not None and actual.get("pid") != pid):
        raise RuntimeError(
            "Running Server PID does not match the selected systemd unit; no service was stopped"
        )
    actual.pop("pid", None)
    if actual != expected:
        raise RuntimeError(
            "Running Server uses different paths; no service was stopped"
        )
    typer.echo("Running Server matches the selected configuration")


@app.command("import-state")
def import_existing_state(
    ctx: typer.Context,
    source_root: Annotated[Path, typer.Option()],
    backup_root: Annotated[Path, typer.Option()],
    component: Annotated[list[str] | None, typer.Option("--component")] = None,
    runtime_source: Path | None = None,
) -> None:
    """Back up and import stopped legacy state into empty configured directories."""
    from cairn.infra.state_import import COMPONENTS, import_state

    settings: ServerConfig = ctx.obj
    backup = import_state(
        settings.paths,
        source_root,
        backup_root,
        components=tuple(component) if component else COMPONENTS,
        runtime_source=runtime_source,
    )
    typer.echo(f"State imported and verified. Original data retained. Backup: {backup}")


def main() -> None:
    try:
        app()
    except (OSError, ValueError, RuntimeError) as error:
        typer.echo(f"Server startup failed: {error}", err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
