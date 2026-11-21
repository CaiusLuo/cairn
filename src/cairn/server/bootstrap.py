"""Starlette runtime host; CLI clients do not import the browser adapter."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
from contextlib import AbstractAsyncContextManager
from datetime import time as wall_time
from pathlib import Path
from typing import Annotated, Any, cast

import typer
import uvicorn
from starlette.responses import JSONResponse
from starlette.routing import Route

from cairn.agent.budget import RunBudget
from cairn.agent.models.config.admission import ProviderApproval, load_approved_provider
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.agent.models.providers.litellm_client import LiteLLMClient
from cairn.agent.models.runtime import ProviderRuntime, build_provider_runtime
from cairn.agent.tracing.tracer import Tracer
from cairn.agent.workspace.lease import WorkspaceLease
from cairn.channels.adapter import ChannelAdapterRegistry
from cairn.channels.discord import discord_adapter_factory
from cairn.channels.feishu import feishu_adapter_factory
from cairn.channels.manager import ChannelManager
from cairn.channels.models import ChannelPlatform
from cairn.channels.qq import qq_adapter_factory
from cairn.channels.replies import ChannelReplyCoordinator
from cairn.channels.store import ChannelStore
from cairn.channels.telegram import telegram_adapter_factory
from cairn.channels.wechat import wechat_legacy_adapter
from cairn.conversation.service import ConversationService
from cairn.conversation.store import DomainStore
from cairn.development.board.service import TaskBoardService
from cairn.development.board.store import TaskBoardStore
from cairn.development.catalogue import RuntimeCatalogue
from cairn.development.execution.runtime import ServiceExecution
from cairn.development.service import TaskService
from cairn.integrations.wechat.delivery import WechatDeliveryCoordinator
from cairn.interfaces.web.app import create_app
from cairn.interfaces.web.assets import default_web_root, require_web_assets
from cairn.interfaces.web.config import WebConfig
from cairn.interfaces.web.management import management_routes
from cairn.interfaces.web.reviews import review_routes
from cairn.knowledge.document_store import DocumentStore
from cairn.knowledge.embeddings import LiteLLMEmbeddingClient
from cairn.knowledge.graph import LLMGraphExtractor
from cairn.knowledge.ingest import IngestionSettings, IngestionWorker
from cairn.knowledge.ocr import OcrSettings, PdfOcrExtractor, rapidocr_recognizer
from cairn.knowledge.retrieval import DocumentSearcher
from cairn.knowledge.store import KnowledgeStore
from cairn.review.channel_delivery import ReviewChannelDispatcher
from cairn.review.collector import ReviewSourceCollector, WeeklySourceCollector
from cairn.review.delivery import ReviewDeliveryCoordinator
from cairn.review.models import DeliveryTarget, StoredReview
from cairn.review.schedule import ReviewScheduleCoordinator
from cairn.review.service import DailyReviewService
from cairn.review.store import ReviewStore
from cairn.review.weekly import WeeklyReviewService
from cairn.review.weekly_schedule import WeeklyReviewScheduleCoordinator
from cairn.server.client import read_runtime_token as read_token
from cairn.server.config import (
    DEFAULT_KNOWLEDGE,
    DEFAULT_REVIEW,
    DEFAULT_SERVICES,
    KnowledgeConfig,
    ReviewConfig,
    ServerConfig,
    Services,
    load_server_config,
)
from cairn.server.lifecycle import running_runtime
from cairn.server.management import ServerControls
from cairn.shared.db import Database

app = typer.Typer(
    pretty_exceptions_show_locals=False,
    help="Run Cairn Server: shared Agent, task queue, HTTP API and Web.",
)


def runtime_app(
    workspace: Path,
    state: Path,
    worktrees: Path,
    *,
    database: Database,
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
    channels_state: Path | None = None,
    host: str = "127.0.0.1",
    services: Services = DEFAULT_SERVICES,
    review: ReviewConfig = DEFAULT_REVIEW,
    knowledge: KnowledgeConfig = DEFAULT_KNOWLEDGE,
    config_file: Path | None = None,
    protected_paths: tuple[Path, ...] = (),
    tracer: Tracer | None = None,
) -> Any:
    if knowledge.embedding_key_env:
        secret_env_keys |= {knowledge.embedding_key_env}
        embedding_key = os.environ.get(knowledge.embedding_key_env)
        if embedding_key:
            sensitive_values = (*sensitive_values, embedding_key)
    catalogue = RuntimeCatalogue(database, sensitive_values=(token, *sensitive_values))
    try:
        execution = ServiceExecution(
            workspace,
            state,
            worktrees,
            OfflineDevelopmentModel
            if provider_runtime is None
            else lambda: provider_runtime.llm,
            database=database,
            budget=budget,
            provider_runtime=provider_runtime,
            secret_env_keys=secret_env_keys,
            sensitive_values=(token, *sensitive_values),
            allow_shell=allow_shell,
            allow_network=allow_network,
            tracer=tracer,
            trace_root=state / "traces",
        )
    except BaseException:
        catalogue.close()
        raise
    service = TaskService(
        catalogue,
        execution,
        board=TaskBoardService(TaskBoardStore(state / "board"), project=str(workspace)),
    )
    domain_store = DomainStore(database)
    service.domain_store = domain_store
    conversations = ConversationService(
        domain_store,
        execution,
        workspace=workspace,
        attachments_root=state / "attachments",
        legacy_service=service,
    )
    execution.service = service
    service.history_reader = execution.history
    service.recovery_reader = execution.recovery
    service.reconciler = execution.reconcile
    try:
        knowledge_store = KnowledgeStore(database)
        document_store: DocumentStore | None = None
        knowledge_worker: IngestionWorker | None = None
        knowledge_embedder: LiteLLMEmbeddingClient | None = None
        if knowledge.enabled:
            originals = state.resolve() / "knowledge" / "documents"
            if originals.resolve() != originals or originals.parent.is_symlink():
                raise ValueError(
                    "document originals directory must be inside runtime state"
                )
            document_store = DocumentStore(
                database,
                originals,
                max_upload_bytes=knowledge.max_upload_bytes,
            )
            if provider_runtime is not None and knowledge.embedding_model:
                knowledge_embedder = _embedding_client(knowledge, provider_runtime)
            graph = None
            if provider_runtime is not None and knowledge.graph_enabled:
                graph = LLMGraphExtractor(
                    cast(LiteLLMClient, provider_runtime.llm),
                    max_chars=knowledge.graph_max_chars,
                )
            knowledge_worker = IngestionWorker(
                document_store,
                embedder=knowledge_embedder,
                graph=graph,
                ocr=_ocr_extractor(knowledge),
                settings=IngestionSettings(
                    chunk_chars=knowledge.chunk_chars,
                    chunk_overlap=knowledge.chunk_overlap,
                    max_chunks=knowledge.max_chunks,
                    extraction_max_chars=knowledge.extraction_max_chars,
                    max_pdf_pages=knowledge.max_pdf_pages,
                    embedding_batch_size=knowledge.embedding_batch_size,
                    graph_max_chunks=knowledge.graph_max_chunks,
                ),
            )
        channel_root = (channels_state or state / "channels").expanduser()
        channel_resolved = channel_root.resolve()
        for writable in (workspace.resolve(), worktrees.resolve()):
            if (
                channel_resolved == writable
                or channel_resolved.is_relative_to(writable)
                or writable.is_relative_to(channel_resolved)
            ):
                raise ValueError(
                    "Channel state must be private and outside writable roots"
                )
        channel_store = ChannelStore(database, channel_root)
        channel_adapters = ChannelAdapterRegistry()
        channel_adapters.register(
            ChannelPlatform.TELEGRAM, telegram_adapter_factory(channel_store)
        )
        channel_adapters.register(
            ChannelPlatform.FEISHU, feishu_adapter_factory(channel_store)
        )
        channel_adapters.register(ChannelPlatform.QQ, qq_adapter_factory(channel_store))
        channel_adapters.register(ChannelPlatform.DISCORD, discord_adapter_factory())
        channel_manager = ChannelManager(
            store=channel_store,
            conversations=conversations,
            adapters=channel_adapters,
        )
        channel_replies = ChannelReplyCoordinator(
            manager=channel_manager,
            runs=conversations,
        )
        conversations.add_run_settled_listener(channel_replies.on_run_settled)
        web = create_app(
            WebConfig(
                workspace=workspace,
                tasks_dir=state / "board",
                knowledge_dir=state / "knowledge",
                host=host,
                port=port,
                auth_passphrase=token,
                web_root=web_root if web_root is not None else default_web_root(),
            ),
            runtime=service,
            conversations=conversations,
            runtime_token=token,
            session_file=state / "web-sessions.json",
            database=database,
            knowledge_store=knowledge_store,
            documents=document_store,
            channels=channel_manager,
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
            database=database,
            token=token,
            web=web,
            port=port,
            wechat_state=wechat_state or state / "wechat",
            agent_home=agent_home or state / "agent",
            schedule_state=schedule_state or state / "schedules",
            channels_state=channel_root,
            knowledge_store=knowledge_store,
            document_store=document_store,
            knowledge_embedder=knowledge_embedder,
            services=services,
            review=review,
            config_file=config_file,
        )
    except BaseException:
        execution.close()
        catalogue.close()
        raise
    channel_manager.attach_legacy(
        wechat_legacy_adapter(
            controls.wechat,
            is_enabled=lambda: controls.state.wechat_enabled,
            set_enabled=controls.set_wechat_enabled,
        )
    )
    channel_manager.set_secret_sink(controls.protect_channel_secrets)
    web.state.channels = channel_manager
    execution.protected_paths += protected_paths
    web.router.routes[0:0] = management_routes(controls, token)
    web.state.controls = controls
    review_target = DeliveryTarget(
        account_id=review.account_id,
        recipient=review.recipient,
    )
    review_store = ReviewStore(knowledge_store)
    review_llm = None if provider_runtime is None else provider_runtime.llm
    review_model_id = (
        None
        if provider_runtime is None
        else provider_runtime.model_manager.current_model_id()
    )
    review_searcher = (
        DocumentSearcher(document_store) if document_store is not None else None
    )
    try:
        reviews = DailyReviewService(
            store=review_store,
            collector=ReviewSourceCollector(
                knowledge=knowledge_store,
                workspace=workspace,
                domain=domain_store,
                documents=document_store,
                searcher=review_searcher,
                memory=controls.memory.store,
                memory_enabled=controls.memory.enabled,
                sensitive_values=execution.sensitive_values,
            ),
            llm=review_llm,
            model_id=review_model_id,
            sensitive_values=execution.sensitive_values,
            delivery_target=review_target if review.recipient else None,
        )
        weekly_reviews = WeeklyReviewService(
            store=review_store,
            collector=WeeklySourceCollector(
                reviews=review_store,
                knowledge=knowledge_store,
                workspace=workspace,
                domain=domain_store,
                documents=document_store,
                searcher=review_searcher,
                memory=controls.memory.store,
                memory_enabled=controls.memory.enabled,
                sensitive_values=execution.sensitive_values,
                cutoff=wall_time.fromisoformat(review.time),
            ),
            llm=review_llm,
            model_id=review_model_id,
            sensitive_values=execution.sensitive_values,
            delivery_target=review_target if review.recipient else None,
            cutoff=wall_time.fromisoformat(review.time),
        )
    except BaseException:
        execution.close()
        catalogue.close()
        raise
    web.router.routes[0:0] = review_routes(
        reviews,
        token,
        weekly_reviews,
        daily_cutoff=wall_time.fromisoformat(review.time),
        host_timezone=review.timezone,
    )
    web.state.reviews = reviews
    web.state.weekly_reviews = weekly_reviews
    delivery = WechatDeliveryCoordinator(
        conversations=conversations,
        wechat=controls.wechat,
        state=controls.wechat.state,
    )
    web.state.delivery = delivery
    controls.delivery = delivery
    review_delivery = (
        ReviewDeliveryCoordinator(
            store=reviews.store,
            wechat=controls.wechat,
            target=review_target,
        )
        if (review.enabled or review.weekly_enabled) and review.recipient
        else None
    )
    web.state.review_delivery = review_delivery
    controls.review_delivery = review_delivery
    review_channel_delivery = ReviewChannelDispatcher(
        store=review_store, channels=channel_manager
    )
    web.state.review_channel_delivery = review_channel_delivery

    async def review_settled(review: StoredReview) -> None:
        if review_delivery is not None:
            review_delivery.rescan()
        await review_channel_delivery.dispatch(review)

    review_schedule = (
        ReviewScheduleCoordinator(
            service=reviews,
            store=controls.schedules.store,
            on_settled=review_settled,
            timezone=review.timezone,
            cutoff=wall_time.fromisoformat(review.time),
        )
        if provider_runtime is not None and review.enabled
        else None
    )
    web.state.review_schedule = review_schedule
    controls.review_schedule = review_schedule
    weekly_review_schedule = (
        WeeklyReviewScheduleCoordinator(
            service=weekly_reviews,
            store=controls.schedules.store,
            on_settled=review_settled,
            timezone=review.timezone,
            cutoff=wall_time.fromisoformat(review.time),
            scheduled_time=wall_time.fromisoformat(review.weekly_time),
        )
        if provider_runtime is not None and review.weekly_enabled
        else None
    )
    web.state.weekly_review_schedule = weekly_review_schedule
    controls.weekly_review_schedule = weekly_review_schedule

    def lifespan(_app: Any) -> AbstractAsyncContextManager[None]:
        return running_runtime(
            execution,
            service,
            conversations,
            controls,
            delivery,
            reviews,
            review_delivery,
            review_schedule,
            channel_manager,
            channel_replies,
            weekly_reviews,
            weekly_review_schedule,
            knowledge_worker,
        )

    web.router.lifespan_context = lifespan
    web.state.runtime = service
    web.state.conversations = conversations
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


def _ocr_extractor(knowledge: KnowledgeConfig) -> PdfOcrExtractor | None:
    if not knowledge.enabled or not knowledge.ocr_enabled:
        return None
    return PdfOcrExtractor(
        rapidocr_recognizer,
        OcrSettings(
            max_pages=knowledge.ocr_max_pages,
            dpi=knowledge.ocr_dpi,
            max_seconds=float(knowledge.ocr_max_seconds),
        ),
    )


def _embedding_client(
    knowledge: KnowledgeConfig, provider_runtime: ProviderRuntime
) -> LiteLLMEmbeddingClient:
    base_url = knowledge.embedding_base_url.rstrip("/")
    key = None
    if knowledge.embedding_key_env:
        key = os.environ.get(knowledge.embedding_key_env)
    elif base_url and base_url == provider_runtime.provider.config.base_url.rstrip("/"):
        key = cast(str | None, getattr(provider_runtime.llm, "api_key", None))
    return LiteLLMEmbeddingClient(
        knowledge.embedding_model,
        api_key=key,
        api_base=base_url,
        dims=knowledge.embedding_dims,
    )


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
        database = Database(config.database_url, namespace=config.database_namespace)
        database.migrate()
        web = runtime_app(
            paths.workspace,
            paths.runtime,
            paths.worktrees,
            database=database,
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
            channels_state=paths.channels,
            services=config.services,
            review=config.review,
            knowledge=config.knowledge,
            config_file=config.config_file,
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
    """Validate bootstrap paths, provider configuration and assets without creating state."""
    settings: ServerConfig = ctx.obj
    validate_config(settings)
    _load_provider(settings)
    typer.echo(f"Configuration OK: {settings.url}; data_root={settings.data_root}")


@app.command("check-running")
def check_running(
    ctx: typer.Context,
    pid: Annotated[int | None, typer.Option("--pid")] = None,
    wait: Annotated[float, typer.Option("--wait", min=0, max=120)] = 0.0,
) -> None:
    """Authenticate the supervised Server, optionally waiting for HTTP startup."""
    from cairn.server.client import RuntimeClient

    settings: ServerConfig = ctx.obj
    paths = settings.paths
    expected = {
        "data_root": str(paths.data_root),
        "workspace": str(paths.workspace),
        "worktrees_root": str(paths.worktrees),
        "url": settings.url,
    }

    async def identity() -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            try:
                api = RuntimeClient(settings.url, read_token(paths.token))
                request = api.request("GET", "bootstrap")
                timeout = min(2.0, max(0.01, deadline - loop.time())) if wait else 20.0
                return await asyncio.wait_for(request, timeout=timeout)
            except Exception:
                if loop.time() >= deadline:
                    raise RuntimeError(
                        "Cannot identify running Server with this configuration; inspect the old service and stop it explicitly before changing data_root/port"
                    ) from None
                await asyncio.sleep(min(0.5, max(0, deadline - loop.time())))

    actual = asyncio.run(identity())
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


@app.command("knowledge-probe")
def knowledge_probe(ctx: typer.Context) -> None:
    """Embed one short text with the configured model; never prints the key."""
    settings: ServerConfig = ctx.obj
    knowledge = settings.knowledge
    if not knowledge.embedding_model:
        raise ValueError("knowledge.embedding_model is not configured")
    provider_runtime, _names, _values = _load_provider(settings)
    if provider_runtime is None:
        raise ValueError("An approved provider is required for a knowledge probe")
    client = _embedding_client(knowledge, provider_runtime)

    async def probe() -> list[float]:
        vectors = await client.embed(["Cairn knowledge embedding probe"])
        return vectors[0]

    vector = asyncio.run(probe())
    typer.echo(f"Embedding OK: model={client.model} dims={len(vector)}")


def main() -> None:
    try:
        app()
    except (OSError, ValueError, RuntimeError) as error:
        typer.echo(f"Server startup failed: {error}", err=True)
        raise SystemExit(1) from None
