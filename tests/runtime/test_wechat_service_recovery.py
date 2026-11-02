"""Offline WeChat iLink closed-loop regressions: no real WeChat or paid model."""

import asyncio
from pathlib import Path

import pytest

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, ToolCall
from cairn.infra.wechat.state import (
    WechatAccount,
    WechatLockError,
    WechatServeLock,
    WechatState,
)
from cairn.terminal.headless import (
    HeadlessResult,
    HeadlessStatus,
    ProviderApproval,
    run_headless_task,
)
from cairn.terminal.wechat.serve import ConversationDecision, ServeConfig, serve_wechat
from tests.support.fake_ilink import FakeILink
from tests.support.runtime import SequenceLLM
from tests.support.wechat_service import (
    TOKEN,
    _account,
    _client,
    _completed,
    _config,
    _msg,
    _task_only_classifier,
)


def test_serve_status_busy_and_cancel(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    calls: list[str] = []

    async def classifier(text: str) -> ConversationDecision:
        return (
            ConversationDecision.CHAT if "进度" in text else ConversationDecision.TASK
        )

    async def slow(prompt: str, cancel: asyncio.Event, task_id: str) -> HeadlessResult:
        calls.append(prompt)
        if prompt == "slow task":
            await asyncio.sleep(30)
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    with FakeILink() as fake:
        fake.updates = [
            {"ret": 0, "msgs": [_msg("slow task", mid=1)], "get_updates_buf": "c1"},
            {
                "ret": 0,
                "msgs": [_msg("/status", mid=2), _msg("进度怎么样", mid=3)],
                "get_updates_buf": "c2",
            },
            {"ret": 0, "msgs": [_msg("another", mid=5)], "get_updates_buf": "c3"},
            {"ret": 0, "msgs": [_msg("/cancel", mid=4)], "get_updates_buf": "c4"},
        ]
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=6),
                client=_client(fake),
                state=WechatState(state_dir),
                task_executor=slow,
                conversation_classifier=classifier,
                allow_insecure_local_http=True,
            )
        )
        replies = "\n".join(str(msg.get("item_list")) for msg in fake.sent)
        assert calls == ["slow task", "another"]
        assert "正在执行" in replies
        assert "已加入队列" in replies
        assert "取消" in replies
        assert replies.count("已收到") == 2
        reloaded = WechatState(state_dir)
        record = reloaded.get_record("msg:1")
        assert record is not None and record["state"] == "cancelled"
        chat = reloaded.get_record("msg:3")
        assert chat is not None and chat["state"] == "chat_replied"


def test_serve_restart_marks_started_as_interrupted_and_never_replays(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.mark("msg:9", "started", sender="u1", context_token="ctx-9", text="old")
    calls: list[str] = []

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    with FakeILink() as fake:
        report = asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=1),
                client=_client(fake),
                state=state,
                task_executor=executor,
                allow_insecure_local_http=True,
            )
        )
        assert calls == []
        assert report.interrupted == 1
        record = state.get_record("msg:9")
        assert record is not None and record["state"] == "interrupted"
        replies = "\n".join(str(msg.get("item_list")) for msg in fake.sent)
        assert "中断" in replies


def test_serve_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first = WechatServeLock(state_dir)
    second = WechatServeLock(state_dir)
    first.acquire(description="first")
    try:
        with pytest.raises(WechatLockError):
            second.acquire(description="second")
    finally:
        first.release()
    second.acquire(description="second")
    second.release()


def test_repeated_cancellation_releases_client_and_lock(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    with FakeILink() as fake:
        config = _config(fake, workspace, state_dir, max_polls=None)

        class _BlockingClient:
            def __init__(self) -> None:
                self.closed = False
                self.started = asyncio.Event()

            async def notify_start(self) -> None:
                return

            async def notify_stop(self) -> None:
                return

            async def get_updates(
                self, cursor: str, **_kwargs: object
            ) -> dict[str, object]:
                self.started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def send_text(self, **_kwargs: object) -> dict[str, object]:
                return {}

            async def close(self) -> None:
                self.closed = True

        client = _BlockingClient()

        async def scenario() -> None:
            task = asyncio.create_task(
                serve_wechat(
                    config=config,
                    client=client,  # type: ignore[arg-type]
                    state=WechatState(state_dir),
                    task_executor=_completed(workspace),
                    allow_insecure_local_http=True,
                )
            )
            await asyncio.wait_for(client.started.wait(), timeout=2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert client.closed is True
        probe = WechatServeLock(state_dir)
        probe.acquire(description="probe")
        probe.release()


def test_recovery_marks_interrupted_but_does_not_message_revoked_sender(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received(
        "msg:revoked",
        sender="revoked",
        context_token="ctx-revoked",
        text="old task",
    )
    state.mark("msg:revoked", "started", sender="revoked", context_token="ctx-revoked")
    with FakeILink() as fake:
        report = asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=1),
                client=_client(fake),
                state=state,
                allow_insecure_local_http=True,
            )
        )
    assert report.interrupted == 1
    assert fake.sent == []
    record = WechatState(state_dir).get_record("msg:revoked")
    assert record is not None and record["state"] == "interrupted"


def test_cancel_during_ack_marks_cancelled_not_started(tmp_path: Path) -> None:
    from cairn.terminal.wechat.serve import ServeReport, _Inbound, _Service

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = WechatState(tmp_path / "state")
    started = asyncio.Event()

    class SlowAckClient:
        def __init__(self) -> None:
            self.calls = 0

        async def send_text(self, **kwargs: object) -> dict[str, object]:
            self.calls += 1
            if "已收到" in str(kwargs.get("text")):
                started.set()
                await asyncio.Event().wait()
            return {}

        async def send_typing(self, **_kwargs: object) -> dict[str, object]:
            return {}

        async def get_config(self, **_kwargs: object) -> dict[str, object]:
            return {"typing_ticket": "t"}

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        raise AssertionError("executor must not run while ack is blocked")

    async def scenario() -> None:
        service = _Service(
            config=type("C", (), {"redact_text": None})(),
            state=state,
            client=SlowAckClient(),  # type: ignore[arg-type]
            executor=executor,
            report=ServeReport(),
            stop_event=None,
        )
        service._start_task(
            "msg:2",
            _Inbound(
                key="msg:2",
                sender="u1",
                context_token="ctx",
                text="do it",
                stable=True,
            ),
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await service._cancel_current() is True
        record = state.get_record("msg:2")
        assert record is not None and record["state"] == "cancelled"
        assert record.get("ack_attempted") is True

    asyncio.run(scenario())


def test_cancel_before_coroutine_starts_is_persisted_cancelled(tmp_path: Path) -> None:
    from cairn.terminal.wechat.serve import ServeReport, _Inbound, _Service

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = WechatState(tmp_path / "state")

    class Client:
        async def send_text(self, **_kwargs: object) -> dict[str, object]:
            return {}

        async def send_typing(self, **_kwargs: object) -> dict[str, object]:
            return {}

        async def get_config(self, **_kwargs: object) -> dict[str, object]:
            return {"typing_ticket": "t"}

    async def executor(*_args: object) -> HeadlessResult:
        raise AssertionError("must not start")

    async def scenario() -> None:
        service = _Service(
            config=type("C", (), {"redact_text": None})(),
            state=state,
            client=Client(),  # type: ignore[arg-type]
            executor=executor,
            report=ServeReport(),
            stop_event=None,
        )
        service._start_task(
            "msg:race",
            _Inbound(
                key="msg:race",
                sender="u1",
                context_token="ctx",
                text="task",
                stable=True,
            ),
        )
        assert await service._cancel_current() is True
        record = state.get_record("msg:race")
        assert record is not None and record["state"] == "cancelled"

    asyncio.run(scenario())


def test_default_wechat_executor_restores_durable_history_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from cairn.core.budget import RunBudget
    from cairn.core.context import ContextBudget
    from cairn.core.models import LLMResponse
    from cairn.infra.wechat.state import WechatAccount
    from cairn.terminal.headless import run_headless_task
    from cairn.terminal.wechat.serve import ServeConfig, default_task_executor
    from tests.support.git import initialize_repository
    from tests.support.runtime import SequenceLLM

    workspace = tmp_path / "ws"
    workspace.mkdir()
    initialize_repository(workspace)
    safe_board = tmp_path.parent / (tmp_path.name + "-board")
    safe_state = tmp_path.parent / (tmp_path.name + "-state")
    monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(safe_board))
    llm = SequenceLLM(
        [LLMResponse(content="first answer"), LLMResponse(content="second answer")]
    )

    async def fake_run(**kwargs: object) -> HeadlessResult:
        return await run_headless_task(
            workspace=workspace,
            prompt=str(kwargs["prompt"]),
            grant_workspace_trust=True,
            grant_shell=False,
            llm=llm,
            budget=RunBudget(max_steps=20),
            context_budget=ContextBudget(max_tokens=100000, response_tokens=1000),
            task_id=str(kwargs["task_id"]),
            durable_session_factory=kwargs.get("durable_session_factory"),  # type: ignore[arg-type]
            provider_name=None,
        )

    monkeypatch.setattr("cairn.terminal.wechat.serve._run_via_config_async", fake_run)
    config = ServeConfig(
        workspace=workspace,
        state_dir=safe_state,
        account=WechatAccount(
            bot_token="bot-token-very-long-123",
            ilink_bot_id="bot",
            base_url="http://127.0.0.1",
        ),
        approval=None,
        allowed_senders=frozenset({"u1"}),
    )

    async def flow() -> None:
        first = default_task_executor(config)
        await first("first prompt", asyncio.Event(), "one", sender="u1")  # type: ignore[call-arg]
        # Simulate service restart: a fresh executor with the same host-private state.
        second = default_task_executor(config)
        await second("second prompt", asyncio.Event(), "two", sender="u1")  # type: ignore[call-arg]

    asyncio.run(flow())
    assert len(llm.calls) == 2
    assert "first prompt" in [m.content for m in llm.calls[1][0]]
    assert "first answer" in [m.content for m in llm.calls[1][0]]


def test_configured_wechat_restores_tool_facts_after_service_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the configured provider and both WeChat service lifecycles offline."""
    from tempfile import TemporaryDirectory

    from cairn.core.context import ContextBudget, ContextBuilder
    from cairn.core.model_manager import ModelManager
    from cairn.core.provider_catalog import NamedProvider
    from cairn.runtime.model_executor import ModelExecutor
    from cairn.runtime.provider_runtime import ProviderRuntime
    from tests.support.git import initialize_repository

    with TemporaryDirectory(
        prefix="cairn-wechat-restart-", dir=Path.home()
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        config_dir = workspace / ".cairn"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text(
            'base_url = "https://example.test/v1"\n'
            'api_key_env = "CAIRN_TEST_WECHAT_KEY"\n'
            '\n[[models]]\nname = "primary"\nmodel_ids = ["fake-model"]\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("CAIRN_TEST_WECHAT_KEY", "fake-provider-secret")
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(root / "board"))
        llm = SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="read-before-restart",
                            name="read_file",
                            arguments={"path": "tracked.txt"},
                        )
                    ]
                ),
                LLMResponse(content="first answer"),
                LLMResponse(content="second answer"),
            ]
        )

        def fake_provider_runtime(
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

            def runtime_factory(_model_id: str) -> tuple[SequenceLLM, ContextBuilder]:
                return llm, builder

            return ProviderRuntime(
                provider=provider,
                model_manager=manager,
                model_executor=ModelExecutor(manager, runtime_factory),
                llm=llm,
                context_builder=builder,
                runtime_factory=runtime_factory,
            )

        monkeypatch.setattr(
            "cairn.terminal.headless.build_provider_runtime", fake_provider_runtime
        )
        approval = ProviderApproval(
            name="default",
            endpoint="https://example.test/v1",
            group="primary",
            key_env="CAIRN_TEST_WECHAT_KEY",
            model_ids=("fake-model",),
        )
        state_dir = root / "state"
        with FakeILink() as fake:
            config = ServeConfig(
                workspace=workspace,
                state_dir=state_dir,
                account=_account(fake),
                approval=approval,
                allowed_senders=frozenset({"u1"}),
                max_steps=20,
                max_polls=100,
                poll_interval=0.02,
                trace_dir=root / "traces",
            )

            async def exchange(message: dict[str, object], cursor: str) -> None:
                fake.updates = [
                    {"ret": 0, "msgs": [message], "get_updates_buf": cursor}
                ]
                await serve_wechat(
                    config=config,
                    allow_insecure_local_http=True,
                    conversation_classifier=_task_only_classifier,
                )

            asyncio.run(exchange(_msg("first question", mid=101), "first"))
            first = WechatState(state_dir).get_record("msg:101")
            assert first is not None and first["state"] == "completed"
            asyncio.run(exchange(_msg("second question", mid=102), "second"))
            second = WechatState(state_dir).get_record("msg:102")
            assert second is not None and second["state"] == "completed"

        assert len(llm.calls) == 3
        messages = llm.calls[2][0]
        assert any(m.role == "user" and m.content == "first question" for m in messages)
        assert any(
            m.role == "assistant"
            and any(call.id == "read-before-restart" for call in m.tool_calls)
            for m in messages
        )
        assert any(
            m.role == "tool"
            and m.tool_call_id == "read-before-restart"
            and "before" in (m.content or "")
            for m in messages
        )
        assert any(
            m.role == "assistant" and m.content == "first answer" for m in messages
        )


def test_wechat_repeated_cancellation_settles_before_close_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tempfile import TemporaryDirectory
    from typing import Any

    from cairn.core.models import Message
    from cairn.runtime.durable_session import DurableSession
    from cairn.terminal.wechat.serve import default_task_executor
    from tests.support.git import initialize_repository

    with TemporaryDirectory(
        prefix="cairn-wechat-cancel-", dir=Path.home()
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(root / "board"))
        config = ServeConfig(
            workspace=workspace,
            state_dir=root / "state",
            account=WechatAccount(
                bot_token="fake-bot-token-very-long",
                ilink_bot_id="bot",
                base_url="https://example.test",
            ),
            approval=None,
            allowed_senders=frozenset({"u1"}),
            max_steps=20,
        )
        started = asyncio.Event()
        settled = asyncio.Event()
        closes = 0
        original_close = DurableSession.close

        class BlockingModel:
            calls = 0

            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        settled.set()
                return LLMResponse(content="later")

        llm = BlockingModel()

        def fail_close_once(session: DurableSession) -> None:
            nonlocal closes
            closes += 1
            assert settled.is_set()
            if closes == 1:
                raise OSError("synthetic close failure")
            original_close(session)

        monkeypatch.setattr(DurableSession, "close", fail_close_once)

        async def fake_run(**kwargs: object) -> HeadlessResult:
            return await run_headless_task(
                workspace=workspace,
                prompt=str(kwargs["prompt"]),
                task_id=str(kwargs["task_id"]),
                grant_workspace_trust=True,
                grant_shell=False,
                llm=llm,
                budget=RunBudget(max_steps=20),
                durable_session_factory=kwargs["durable_session_factory"],  # type: ignore[arg-type]
            )

        monkeypatch.setattr(
            "cairn.terminal.wechat.serve._run_via_config_async", fake_run
        )

        async def flow() -> HeadlessResult:
            execute = default_task_executor(config)
            pending: asyncio.Future[HeadlessResult] = asyncio.ensure_future(
                execute("first", asyncio.Event(), "one", sender="u1")  # type: ignore[call-arg]
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            pending.cancel("first cancellation")
            await asyncio.sleep(0)
            pending.cancel("second cancellation")
            with pytest.raises(asyncio.CancelledError) as caught:
                await pending
            assert caught.value.args == ("first cancellation",)
            assert "cleanup" in " ".join(getattr(caught.value, "__notes__", ())).lower()
            assert settled.is_set()
            return await execute("later", asyncio.Event(), "two", sender="u1")  # type: ignore[call-arg]

        later = asyncio.run(flow())
        assert closes >= 2
        assert later.status is HeadlessStatus.COMPLETED
        assert llm.calls == 2


def test_queued_messages_resume_after_restart_without_replaying_started(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    calls: list[str] = []

    async def flow(fake: FakeILink) -> None:
        state = WechatState(state_dir)
        started = asyncio.Event()
        stop_first = asyncio.Event()

        async def first_executor(
            prompt: str, cancel: asyncio.Event, task_id: str
        ) -> HeadlessResult:
            calls.append(prompt)
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        batch = {
            "ret": 0,
            "msgs": [
                _msg("interrupted", mid=99),
                _msg("A", mid=10),
                _msg("B", mid=2),
                _msg("C", mid=1),
            ],
            "get_updates_buf": "c1",
        }
        fake.updates = [batch]
        config = _config(fake, workspace, state_dir, max_polls=None)
        service = asyncio.create_task(
            serve_wechat(
                config=config,
                client=_client(fake),
                state=state,
                task_executor=first_executor,
                stop_event=stop_first,
                allow_insecure_local_http=True,
            )
        )

        async def wait_for_queued() -> None:
            while not all(
                (state.get_record(key) or {}).get("state") == "queued"
                for key in ("msg:10", "msg:2", "msg:1")
            ):
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await asyncio.wait_for(wait_for_queued(), timeout=5)
        finally:
            stop_first.set()
            await asyncio.wait_for(service, timeout=5)

        assert calls == ["interrupted"]
        reloaded = WechatState(state_dir)
        assert (reloaded.get_record("msg:99") or {}).get("state") == "cancelled"
        assert all(
            (reloaded.get_record(key) or {}).get("state") == "queued"
            for key in ("msg:10", "msg:2", "msg:1")
        )

        async def later_executor(
            prompt: str, cancel: asyncio.Event, task_id: str
        ) -> HeadlessResult:
            calls.append(prompt)
            return HeadlessResult(
                task_id=task_id,
                status=HeadlessStatus.COMPLETED,
                workspace=str(workspace),
                final_response="done",
            )

        fake.updates = [batch]
        await serve_wechat(
            config=_config(fake, workspace, state_dir, max_polls=80),
            client=_client(fake),
            state=reloaded,
            task_executor=later_executor,
            allow_insecure_local_http=True,
        )

    with FakeILink() as fake:
        asyncio.run(flow(fake))
        receipts = "\n".join(str(message.get("item_list")) for message in fake.sent)
        assert receipts.count("已收到") == 4
    assert calls == ["interrupted", "A", "B", "C"]
    reloaded = WechatState(state_dir)
    assert all(
        (reloaded.get_record(key) or {}).get("state") == "completed"
        for key in ("msg:10", "msg:2", "msg:1")
    )


def test_queued_work_starts_while_poll_is_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = WechatState(tmp_path / "state")

    async def flow() -> None:
        first_started = asyncio.Event()
        finish_first = asyncio.Event()
        second_started = asyncio.Event()
        poll_waiting = asyncio.Event()
        release_poll = asyncio.Event()
        stop = asyncio.Event()

        class BlockingClient:
            calls = 0

            async def notify_start(self) -> None:
                return None

            async def notify_stop(self) -> None:
                return None

            async def get_updates(
                self, cursor: str, **kwargs: object
            ) -> dict[str, object]:
                self.calls += 1
                if self.calls == 1:
                    return {
                        "ret": 0,
                        "msgs": [_msg("first", mid=1), _msg("second", mid=2)],
                        "get_updates_buf": "c1",
                    }
                poll_waiting.set()
                await release_poll.wait()
                return {"ret": 0, "msgs": [], "get_updates_buf": cursor}

            async def send_text(self, **kwargs: object) -> None:
                return None

            async def get_config(self, **kwargs: object) -> dict[str, object]:
                return {}

            async def send_typing(self, **kwargs: object) -> None:
                return None

            async def close(self) -> None:
                return None

        async def execute(
            prompt: str, cancel: asyncio.Event, task_id: str
        ) -> HeadlessResult:
            if prompt == "first":
                first_started.set()
                await finish_first.wait()
            else:
                second_started.set()
            return HeadlessResult(
                task_id=task_id,
                status=HeadlessStatus.COMPLETED,
                workspace=str(workspace),
            )

        client = BlockingClient()
        config = ServeConfig(
            workspace=workspace,
            state_dir=state.root,
            account=WechatAccount(
                bot_token=TOKEN, ilink_bot_id="bot", base_url="http://127.0.0.1:1"
            ),
            approval=None,
            allowed_senders=frozenset({"u1"}),
            max_polls=None,
        )
        service = asyncio.create_task(
            serve_wechat(
                config=config,
                client=client,  # type: ignore[arg-type]
                state=state,
                task_executor=execute,
                stop_event=stop,
                allow_insecure_local_http=True,
            )
        )
        try:
            await asyncio.wait_for(first_started.wait(), timeout=5)
            await asyncio.wait_for(poll_waiting.wait(), timeout=5)
            finish_first.set()
            await asyncio.wait_for(second_started.wait(), timeout=5)
            while (state.get_record("msg:2") or {}).get("state") != "completed":
                await asyncio.sleep(0.01)
        finally:
            stop.set()
            release_poll.set()
            finish_first.set()
            await asyncio.wait_for(service, timeout=5)

    asyncio.run(flow())


def test_recovery_checks_queued_sender_and_does_not_replay_started(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    for key, sender, text, status in (
        ("msg:99", "u1", "interrupted", "started"),
        ("msg:10", "revoked", "revoked task", "queued"),
        ("msg:2", "u1", "valid task", "queued"),
    ):
        state.record_received(key, sender=sender, context_token="ctx", text=text)
        state.mark(key, status, ack_attempted=True)
    calls: list[str] = []

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    with FakeILink() as fake:
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=50),
                client=_client(fake),
                state=WechatState(state_dir),
                task_executor=execute,
                allow_insecure_local_http=True,
            )
        )
        assert not any(message.get("to_user_id") == "revoked" for message in fake.sent)
    reopened = WechatState(state_dir)
    assert calls == ["valid task"]
    assert (reopened.get_record("msg:99") or {}).get("state") == "interrupted"
    assert (reopened.get_record("msg:10") or {}).get("state") == "rejected"
    assert (reopened.get_record("msg:2") or {}).get("state") == "completed"


def test_recovered_non_text_message_never_starts_task(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received(
        "msg:image", sender="u1", context_token="ctx", text="", is_text=False
    )
    calls: list[str] = []

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    with FakeILink() as fake:
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=5),
                client=_client(fake),
                state=WechatState(state_dir),
                task_executor=execute,
                allow_insecure_local_http=True,
            )
        )
    assert calls == []
    assert (WechatState(state_dir).get_record("msg:image") or {}).get(
        "state"
    ) == "rejected"


def test_recovery_does_not_resend_an_uncertain_queue_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received(
        "msg:pending", sender="u1", context_token="ctx", text="queued task"
    )
    state.mark("msg:pending", "queueing", ack_attempted=True)
    calls: list[str] = []

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    with FakeILink() as fake:
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=50),
                client=_client(fake),
                state=WechatState(state_dir),
                task_executor=execute,
                allow_insecure_local_http=True,
            )
        )
        replies = "\n".join(str(message.get("item_list")) for message in fake.sent)
        assert "已收到" not in replies
    assert calls == ["queued task"]
    record = WechatState(state_dir).get_record("msg:pending")
    assert record is not None and record["state"] == "completed"
    assert record["ack_state"] == "unknown"


def test_damaged_queued_record_fails_before_start_and_releases_lock(
    tmp_path: Path,
) -> None:
    from cairn.infra.wechat.state import WechatServeLock

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received("msg:bad", sender="u1", context_token="ctx", text="task")
    state.mark("msg:bad", "queued")
    state.load()["messages"]["msg:bad"].pop("text")
    state.flush()

    with FakeILink() as fake, pytest.raises(ValueError, match="damaged"):
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=1),
                client=_client(fake),
                state=state,
                task_executor=_completed(workspace),
                allow_insecure_local_http=True,
            )
        )
    probe = WechatServeLock(state_dir)
    probe.acquire(description="probe")
    probe.release()


def test_recovery_cancellation_waits_for_started_task_cleanup(
    tmp_path: Path,
) -> None:
    from cairn.infra.wechat.state import WechatServeLock

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received("msg:start", sender="u1", context_token="ctx", text="task")
    started = asyncio.Event()

    async def blocking(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with FakeILink() as fake:

        async def scenario() -> None:
            task = asyncio.create_task(
                serve_wechat(
                    config=_config(fake, workspace, state_dir, max_polls=None),
                    client=_client(fake),
                    state=state,
                    task_executor=blocking,
                    allow_insecure_local_http=True,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
    probe = WechatServeLock(state_dir)
    probe.acquire(description="probe")
    probe.release()


def test_damaged_boolean_recovery_field_fails_before_start(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received("msg:bad-bool", sender="u1", context_token="ctx", text="task")
    state.load()["messages"]["msg:bad-bool"]["stable"] = "false"
    state.flush()

    with FakeILink() as fake, pytest.raises(ValueError, match="damaged"):
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=1),
                client=_client(fake),
                state=state,
                task_executor=_completed(workspace),
                allow_insecure_local_http=True,
            )
        )


@pytest.mark.parametrize("field", ["stable", "is_text"])
@pytest.mark.parametrize("state_name", ["received", "queueing", "queued"])
@pytest.mark.parametrize("bad", [None, "false", 1])
def test_recovery_rejects_explicit_null_or_wrong_boolean(
    tmp_path: Path, field: str, state_name: str, bad: object
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received("msg:bad", sender="u1", context_token="ctx", text="task")
    state.load()["messages"]["msg:bad"]["state"] = state_name
    state.load()["messages"]["msg:bad"][field] = bad
    state.flush()
    calls: list[str] = []

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    with FakeILink() as fake:
        with pytest.raises(ValueError, match="damaged"):
            asyncio.run(
                serve_wechat(
                    config=_config(fake, workspace, state_dir, max_polls=1),
                    client=_client(fake),
                    state=state,
                    task_executor=execute,
                    allow_insecure_local_http=True,
                )
            )
        assert calls == []
        assert fake.sent == []


def test_recovery_missing_legacy_booleans_still_defaults(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received("msg:legacy", sender="u1", context_token="ctx", text="task")
    state.load()["messages"]["msg:legacy"].pop("stable", None)
    state.load()["messages"]["msg:legacy"].pop("is_text", None)
    state.flush()
    calls: list[str] = []

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    with FakeILink() as fake:
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=20),
                client=_client(fake),
                state=state,
                task_executor=execute,
                allow_insecure_local_http=True,
            )
        )
    assert calls == ["task"]


def test_recovery_validates_whole_set_before_start_or_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received(
        "msg:good", sender="u1", context_token="ctx", text="good task"
    )
    state.record_received("msg:bad", sender="u1", context_token="ctx", text="bad task")
    state.load()["messages"]["msg:bad"]["stable"] = None
    state.flush()
    calls: list[str] = []

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    with FakeILink() as fake:
        with pytest.raises(ValueError, match="damaged"):
            asyncio.run(
                serve_wechat(
                    config=_config(fake, workspace, state_dir, max_polls=1),
                    client=_client(fake),
                    state=state,
                    task_executor=execute,
                    allow_insecure_local_http=True,
                )
            )
        assert calls == []
        assert fake.sent == []
    assert (state.get_record("msg:good") or {}).get("state") == "received"


def test_poll_updates_cancels_owned_poll_and_stop_waiter(tmp_path: Path) -> None:
    from cairn.terminal.wechat.serve import ServeReport, _Service

    class PollClient:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.finished = asyncio.Event()

        async def get_updates(
            self, *args: object, **kwargs: object
        ) -> dict[str, object]:
            self.started.set()
            try:
                await asyncio.sleep(30)
                return {}
            finally:
                self.finished.set()

    async def scenario() -> None:
        client = PollClient()
        stop = asyncio.Event()
        service = _Service(
            config=type("C", (), {"poll_timeout_ms": None})(),
            state=WechatState(tmp_path / "state"),
            client=client,  # type: ignore[arg-type]
            executor=lambda *args: None,  # type: ignore[arg-type]
            report=ServeReport(),
            stop_event=stop,
        )
        task = asyncio.create_task(service._poll_updates())
        await asyncio.wait_for(client.started.wait(), timeout=2)
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.finished.is_set()

    asyncio.run(scenario())
