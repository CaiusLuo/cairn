"""Real offline Store/Agent regressions for WeChat durable ownership."""

import asyncio
import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.model_manager import ModelManager
from cairn.core.models import LLMResponse, Message
from cairn.core.provider_catalog import NamedProvider
from cairn.infra.sessions.host import observe_workspace
from cairn.infra.sessions.models import SessionBinding, ToolFence, TurnIntent
from cairn.infra.workspace.workspace import Workspace
from cairn.integrations.wechat.serve import (
    ServeConfig,
    default_conversation_executor,
    default_task_executor,
)
from cairn.integrations.wechat.state import WechatAccount
from cairn.runtime.model_executor import ModelExecutor
from cairn.runtime.provider_runtime import ProviderRuntime
from cairn.terminal.durable import open_store
from cairn.terminal.headless import HeadlessStatus, ProviderApproval
from tests.support.git import initialize_repository
from tests.support.runtime import SequenceLLM
from tests.support.sessions import intent, settled


def _session_id(config: ServeConfig, purpose: str | None) -> str:
    suffix = "" if purpose is None else f":{purpose}"
    digest = hashlib.sha256(f"wechat:{config.workspace}:u1{suffix}".encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


@pytest.fixture
def configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ServeConfig, SequenceLLM]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initialize_repository(workspace)
    project = workspace / ".cairn"
    project.mkdir()
    (project / "models.toml").write_text(
        'base_url = "https://example.test/v1"\n'
        'api_key_env = "CAIRN_TEST_WECHAT_KEY"\n'
        '\n[[models]]\nname = "primary"\nmodel_ids = ["fake-model"]\n'
    )
    monkeypatch.setenv("CAIRN_TEST_WECHAT_KEY", "fake-provider-secret")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(tmp_path / "board"))
    llm = SequenceLLM([])

    def provider_runtime(
        provider: NamedProvider,
        credential: str | None,
        context_budget: ContextBudget,
        *,
        group: str | None = None,
    ) -> ProviderRuntime:
        assert credential == "fake-provider-secret"
        manager = ModelManager(provider.config)
        if group is not None:
            manager.select_model(group)
        builder = ContextBuilder(context_budget)

        def factory(_model_id: str) -> tuple[SequenceLLM, ContextBuilder]:
            return llm, builder

        return ProviderRuntime(
            provider=provider,
            model_manager=manager,
            model_executor=ModelExecutor(manager, factory),
            llm=llm,
            context_builder=builder,
            runtime_factory=factory,
        )

    monkeypatch.setattr(
        "cairn.terminal.headless.build_provider_runtime", provider_runtime
    )
    approval = ProviderApproval(
        name="default",
        endpoint="https://example.test/v1",
        group="primary",
        key_env="CAIRN_TEST_WECHAT_KEY",
        model_ids=("fake-model",),
    )
    config = ServeConfig(
        workspace=workspace,
        state_dir=tmp_path / "state",
        account=WechatAccount(
            bot_token="fake-bot-token",
            ilink_bot_id="bot",
            base_url="https://example.test",
        ),
        approval=approval,
        allowed_senders=frozenset({"u1"}),
        max_steps=150,
        trace_dir=tmp_path / "traces",
    )
    yield config, llm


def test_chat_uses_same_host_configured_step_binding_as_task(
    configured: tuple[ServeConfig, SequenceLLM],
) -> None:
    config, llm = configured
    llm.responses = [LLMResponse(content="task done"), LLMResponse(content="hello")]
    task = default_task_executor(config)
    chat = default_conversation_executor(config, task.durable_factory)  # type: ignore[attr-defined]

    async def scenario() -> None:
        try:
            result = await task("task", asyncio.Event(), "one", sender="u1")  # type: ignore[call-arg]
            assert result.status is HeadlessStatus.COMPLETED
            assert await chat("hello", "u1", "") == "hello"
        finally:
            task.close()  # type: ignore[attr-defined]

    asyncio.run(scenario())
    assert len(llm.calls) == 2
    reader = open_store(
        config.state_dir / "sessions", Workspace(config.workspace), inspect=True
    )
    try:
        task_report = reader.inspect(_session_id(config, "task"))
        chat_report = reader.inspect(_session_id(config, "chat"))
        assert task_report.binding.provider is not None
        assert task_report.binding.provider.max_steps == 150
        assert chat_report.binding.provider is not None
        assert chat_report.binding.provider.max_steps == 150
    finally:
        reader.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_task_is_not_abandoned_and_does_not_block_chat(
    configured: tuple[ServeConfig, SequenceLLM], legacy: bool
) -> None:
    config, llm = configured
    task_id = _session_id(config, None if legacy else "task")
    store = open_store(config.state_dir / "sessions", Workspace(config.workspace))
    binding = SessionBinding(
        session_id=task_id,
        workspace=observe_workspace(Workspace(config.workspace), frozenset()),
        provider=None,
        observed_at_utc="2026-01-01T00:00:00Z",
    )
    receipt = store.create(binding)
    historical = intent(
        binding, receipt.revision, user="historical prompt", time="2026-01-01T00:00:01Z"
    )
    history = [
        Message(role="user", content="historical prompt"),
        Message(role="assistant", content="historical answer"),
    ]
    receipt = store.commit_turn(
        store.begin_turn(receipt.revision, historical),
        settled(historical, history=history, time="2026-01-01T00:00:02Z"),
    )
    binding = store.inspect(task_id).binding
    turn_id = str(uuid.uuid4())
    active = store.begin_turn(
        receipt.revision,
        TurnIntent(
            session_id=task_id,
            turn_id=turn_id,
            base_seq=1,
            message_start=2,
            input_digest=hashlib.sha256(b"unknown task").hexdigest(),
            started_at_utc="2026-01-01T00:00:03Z",
            binding=binding,
        ),
    )
    active = store.fence_tool(
        active,
        ToolFence(
            session_id=task_id,
            turn_id=turn_id,
            ordinal=1,
            assistant_message_index=3,
            call_id="unknown-write",
            tool_name="bash",
            observed_at_utc="2026-01-01T00:00:04Z",
        ),
    )
    active = store.require_recovery(active, "finalization_failed")
    store.release_turn(active, turn_id)
    before = store.inspect(task_id)
    store.close()
    llm.responses = [LLMResponse(content="chat available")]
    task = default_task_executor(config)
    chat = default_conversation_executor(config, task.durable_factory)  # type: ignore[attr-defined]

    async def scenario() -> None:
        try:
            blocked = await task(
                "continue unknown task", asyncio.Event(), "two", sender="u1"
            )  # type: ignore[call-arg]
            assert blocked.status is HeadlessStatus.RUNTIME_ERROR
            assert "recovery_required" in (blocked.error or "")
            text = await chat("hello", "u1", "")
            assert "chat available" in text
            if legacy:
                assert "独立聊天" in text and task_id in text
        finally:
            task.close()  # type: ignore[attr-defined]

    asyncio.run(scenario())
    assert len(llm.calls) == 1
    reader = open_store(
        config.state_dir / "sessions", Workspace(config.workspace), inspect=True
    )
    try:
        after = reader.inspect(task_id)
        assert after.revision == before.revision
        assert after.checkpoint == before.checkpoint
        assert after.attempts == before.attempts
        assert after.unresolved is not None
        assert after.unresolved.diagnostics.fences[0].call_id == "unknown-write"
    finally:
        reader.close()


def test_existing_separate_chat_store_requires_explicit_migration(
    configured: tuple[ServeConfig, SequenceLLM],
) -> None:
    config, llm = configured
    separate = config.state_dir / "sessions-chat"
    separate.mkdir(parents=True)
    original = b"retained chat database; do not overwrite"
    (separate / "sessions.sqlite3").write_bytes(original)
    llm.responses = [LLMResponse(content="task works")]
    task = default_task_executor(config)
    chat = default_conversation_executor(config, task.durable_factory)  # type: ignore[attr-defined]

    async def scenario() -> None:
        from cairn.integrations.wechat.serve import ConversationExecutionError

        try:
            with pytest.raises(
                ConversationExecutionError, match="chat_store_migration_required"
            ):
                await chat("hello", "u1", "")
            result = await task("task", asyncio.Event(), "one", sender="u1")  # type: ignore[call-arg]
            assert result.status is HeadlessStatus.COMPLETED
        finally:
            task.close()  # type: ignore[attr-defined]

    asyncio.run(scenario())
    assert (separate / "sessions.sqlite3").read_bytes() == original
    assert len(llm.calls) == 1


def test_chat_can_finish_while_task_owns_same_store_writer(
    configured: tuple[ServeConfig, SequenceLLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, llm = configured
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(
        messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        llm.calls.append((messages, tools))
        if messages[-1].content == "long task":
            started.set()
            await release.wait()
            return LLMResponse(content="task done")
        return LLMResponse(content="chat done")

    monkeypatch.setattr(llm, "generate", generate)
    task = default_task_executor(config)
    chat = default_conversation_executor(config, task.durable_factory)  # type: ignore[attr-defined]

    async def scenario() -> None:
        request = task("long task", asyncio.Event(), "one", sender="u1")  # type: ignore[call-arg]
        pending = asyncio.ensure_future(request)
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            assert (
                await asyncio.wait_for(chat("hello", "u1", ""), timeout=5)
                == "chat done"
            )
            assert not pending.done()
            release.set()
            assert (await pending).status is HeadlessStatus.COMPLETED
        finally:
            release.set()
            await pending
            task.close()  # type: ignore[attr-defined]

    asyncio.run(scenario())
    assert len(llm.calls) == 2


@pytest.mark.parametrize("cancellation", ["none", "primary", "late"])
def test_service_settles_failed_cleanup_before_closing_shared_writer(
    configured: tuple[ServeConfig, SequenceLLM],
    monkeypatch: pytest.MonkeyPatch,
    cancellation: str,
) -> None:
    from dataclasses import replace

    from cairn.integrations.wechat.serve import ConversationDecision, serve_wechat
    from tests.support.wechat_service import _msg

    config, llm = configured
    config = replace(config, max_polls=None, poll_interval=0.01)
    llm.responses = [LLMResponse(content="task done")]
    stop = asyncio.Event()
    task_finished, cleanup_entered, release_cleanup = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    class Client:
        polled = False
        closed = False

        async def notify_start(self) -> None:
            return

        async def notify_stop(self) -> None:
            return

        async def get_updates(self, cursor: str, **kwargs: object) -> dict[str, object]:
            if not self.polled:
                self.polled = True
                return {
                    "ret": 0,
                    "msgs": [_msg("task", mid=101)],
                    "get_updates_buf": "one",
                }
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def send_text(self, **kwargs: object) -> dict[str, object]:
            if "状态: completed" in str(kwargs.get("text")):
                task_finished.set()
                if cancellation != "primary":
                    stop.set()
            return {}

        async def close(self) -> None:
            self.closed = True

    class Control:
        def __init__(self, *args: object) -> None:
            pass

        async def start(self) -> None:
            return

        async def close(self) -> None:
            cleanup_entered.set()
            await release_cleanup.wait()
            raise OSError("private-secret-do-not-leak")

    async def classifier(text: str) -> ConversationDecision:
        return ConversationDecision.TASK

    monkeypatch.setattr("cairn.integrations.wechat.serve.WechatControlServer", Control)
    client = Client()

    async def scenario() -> None:
        pending = asyncio.create_task(
            serve_wechat(
                config=config,
                client=client,  # type: ignore[arg-type]
                stop_event=stop,
                conversation_classifier=classifier,
            )
        )
        await asyncio.wait_for(task_finished.wait(), timeout=5)
        if cancellation == "primary":
            pending.cancel("first stop")
        await asyncio.wait_for(cleanup_entered.wait(), timeout=5)
        if cancellation != "none":
            pending.cancel("late stop")
            await asyncio.sleep(0)
            pending.cancel("repeated stop")
            await asyncio.sleep(0)
            assert not pending.done()
        release_cleanup.set()
        if cancellation == "none":
            with pytest.raises(
                RuntimeError, match="WeChat service cleanup failed"
            ) as failed:
                await pending
            assert "private-secret" not in str(failed.value)
        else:
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await pending
            assert cancelled.value.args == (
                ("first stop",) if cancellation == "primary" else ("late stop",)
            )
            assert "private-secret" not in str(cancelled.value)
            assert "private-secret" not in str(
                getattr(cancelled.value, "__notes__", ())
            )
        assert client.closed
        # A new real writer can acquire only after the service closed the old
        # borrowed Store. The committed result survives the injected close error.
        writer = open_store(config.state_dir / "sessions", Workspace(config.workspace))
        try:
            assert writer.inspect(_session_id(config, "task")).checkpoint.seq == 1
        finally:
            writer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancelled", [False, True])
def test_chat_primary_failure_keeps_cleanup_note_and_retained_writer(
    configured: tuple[ServeConfig, SequenceLLM],
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    from cairn.integrations.wechat import serve as serve_module
    from cairn.runtime.durable_session import DurableSession

    config, llm = configured
    llm.responses = [LLMResponse(content="settled chat")]
    from cairn.terminal.headless import _run_via_config_async

    original_run = _run_via_config_async
    original_close = DurableSession.close
    close_enabled = False
    closes = 0
    primary: BaseException = (
        asyncio.CancelledError("original stop")
        if cancelled
        else RuntimeError("original failure")
    )

    async def fail_after_turn(**kwargs: Any) -> object:
        nonlocal close_enabled
        await original_run(**kwargs)
        close_enabled = True
        raise primary

    def fail_close_once(session: DurableSession) -> None:
        nonlocal closes
        if close_enabled:
            closes += 1
            if closes == 1:
                raise OSError("private-cleanup-secret")
        original_close(session)

    monkeypatch.setattr(serve_module, "_run_via_config_async", fail_after_turn)
    monkeypatch.setattr(DurableSession, "close", fail_close_once)
    task = default_task_executor(config)
    chat = default_conversation_executor(config, task.durable_factory)  # type: ignore[attr-defined]

    async def scenario() -> None:
        try:
            with pytest.raises(type(primary)) as failed:
                await chat("hello", "u1", "")
            assert failed.value is primary
            assert failed.value.args == primary.args
            notes = str(getattr(failed.value, "__notes__", ()))
            assert "Durable session cleanup failed: OSError" in notes
            assert "private-cleanup-secret" not in notes
        finally:
            task.close()  # type: ignore[attr-defined]

    asyncio.run(scenario())
    assert closes == 2
    # The retained close was retried by the same host; its writer is now free.
    store = open_store(config.state_dir / "sessions", Workspace(config.workspace))
    try:
        assert store.inspect(_session_id(config, "chat")).checkpoint.seq == 1
    finally:
        store.close()


def test_late_cancel_during_worker_cleanup_is_not_reported_as_success(
    configured: tuple[ServeConfig, SequenceLLM],
) -> None:
    from dataclasses import replace

    from cairn.integrations.wechat.serve import ConversationDecision, serve_wechat
    from cairn.terminal.headless import HeadlessResult
    from tests.support.wechat_service import _msg

    config, _llm = configured
    config = replace(config, max_polls=None)
    stop = asyncio.Event()
    started, cleanup_entered, release, cleaned = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    class Client:
        polled = False
        closed = False
        notified = False

        async def notify_start(self) -> None:
            return

        async def notify_stop(self) -> None:
            self.notified = True

        async def get_updates(self, cursor: str, **kwargs: object) -> dict[str, object]:
            if not self.polled:
                self.polled = True
                return {
                    "ret": 0,
                    "msgs": [_msg("task", mid=123)],
                    "get_updates_buf": "one",
                }
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def send_text(self, **kwargs: object) -> dict[str, object]:
            return {}

        async def close(self) -> None:
            assert cleaned.is_set()
            self.closed = True

    async def classifier(text: str) -> ConversationDecision:
        return ConversationDecision.TASK

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        finally:
            cleanup_entered.set()
            await release.wait()
            cleaned.set()

    client = Client()

    async def scenario() -> None:
        pending = asyncio.create_task(
            serve_wechat(
                config=config,
                client=client,  # type: ignore[arg-type]
                task_executor=executor,
                conversation_classifier=classifier,
                stop_event=stop,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        stop.set()
        await asyncio.wait_for(cleanup_entered.wait(), timeout=5)
        pending.cancel("late stop while worker cleanup")
        await asyncio.sleep(0)
        pending.cancel("repeated stop")
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await pending
        assert cancelled.value.args == ("late stop while worker cleanup",)
        assert cleaned.is_set() and client.closed and client.notified

    asyncio.run(scenario())


def test_recovery_failure_settles_already_owned_chat_and_intent_work(
    configured: tuple[ServeConfig, SequenceLLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.integrations.wechat import serve as serve_module

    config, _llm = configured
    started = [asyncio.Event(), asyncio.Event()]
    settled: list[int] = []
    children: list[asyncio.Task[None]] = []
    primary = RuntimeError("original recovery failure")

    async def work(index: int) -> None:
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.append(index)

    async def broken_recovery(service: Any) -> None:
        children.extend([asyncio.create_task(work(0)), asyncio.create_task(work(1))])
        service._intent_tasks.add(children[0])
        service._chat_tasks.add(children[1])
        await asyncio.gather(*(event.wait() for event in started))
        raise primary

    class Client:
        closed = False
        notified = False

        async def notify_start(self) -> None:
            return

        async def notify_stop(self) -> None:
            assert sorted(settled) == [0, 1]
            self.notified = True

        async def close(self) -> None:
            assert all(child.done() for child in children)
            self.closed = True

    monkeypatch.setattr(serve_module._Service, "_recover", broken_recovery)
    client = Client()

    async def executor(prompt: str, cancel: asyncio.Event, task_id: str) -> object:
        raise AssertionError("no execution")

    async def scenario() -> None:
        with pytest.raises(RuntimeError) as failed:
            await serve_module.serve_wechat(
                config=config,
                client=client,  # type: ignore[arg-type]
                task_executor=executor,  # type: ignore[arg-type]
            )
        assert failed.value is primary
        assert client.closed and client.notified
        assert sorted(settled) == [0, 1]

    asyncio.run(scenario())
