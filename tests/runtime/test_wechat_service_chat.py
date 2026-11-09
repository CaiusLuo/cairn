"""Offline WeChat iLink closed-loop regressions: no real WeChat or paid model."""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import pytest

from cairn.core.models import ToolResult
from cairn.development.execution.headless import (
    HeadlessResult,
    HeadlessStatus,
)
from cairn.infra.tools.base import ToolExecutionContext
from cairn.infra.tools.lease import LeaseTool
from cairn.integrations.wechat.serve import (
    ConversationDecision,
    ConversationExecutionError,
    ServeConfig,
    close_conversation_sessions,
    conversation_text,
    default_conversation_executor,
    default_task_executor,
    serve_wechat,
)
from cairn.integrations.wechat.state import (
    WechatAccount,
    WechatState,
)
from cairn.runtime.durable_session import DurableSession
from tests.support.fake_ilink import FakeILink
from tests.support.wechat_service import (
    _client,
    _completed,
    _config,
    _msg,
)


def test_busy_progress_chat_answers_without_waiting_or_duplicate_task(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    calls: list[str] = []
    chat_calls: list[tuple[str, str, str]] = []
    release = asyncio.Event()
    worker_started = asyncio.Event()

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        worker_started.set()
        await release.wait()
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    async def chat(question: str, sender: str, snapshot: str) -> str:
        chat_calls.append((question, sender, snapshot))
        assert "任务摘要" in snapshot
        return "任务仍在运行\uff0c进度正常。"

    async def classifier(text: str) -> ConversationDecision:
        return (
            ConversationDecision.CHAT if "进度" in text else ConversationDecision.TASK
        )

    async def scenario() -> None:
        with FakeILink() as fake:
            fake.updates = [
                {
                    "ret": 0,
                    "msgs": [_msg("slow task", mid=1), _msg("进度怎么样", mid=2)],
                    "get_updates_buf": "c1",
                }
            ]
            stop = asyncio.Event()
            service = asyncio.create_task(
                serve_wechat(
                    config=_config(fake, workspace, state_dir, max_polls=20),
                    client=_client(fake),
                    state=WechatState(state_dir),
                    task_executor=execute,
                    conversation_executor=chat,
                    conversation_classifier=classifier,
                    stop_event=stop,
                    allow_insecure_local_http=True,
                )
            )
            try:
                await asyncio.wait_for(worker_started.wait(), timeout=3)
                for _ in range(100):
                    if any(
                        "任务仍在运行" in str(m.get("item_list")) for m in fake.sent
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert any("任务仍在运行" in str(m.get("item_list")) for m in fake.sent)
                assert calls == ["slow task"]
                assert chat_calls and chat_calls[0][1] == "u1"
                assert not release.is_set()
            finally:
                release.set()
                stop.set()
                await asyncio.wait_for(service, timeout=5)

    asyncio.run(scenario())


def test_status_command_bypasses_slow_classifier(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    classifier_started = asyncio.Event()
    release_classifier = asyncio.Event()
    worker_started = asyncio.Event()

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        worker_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def classifier(text: str) -> ConversationDecision:
        if "slow task" in text:
            return ConversationDecision.TASK
        classifier_started.set()
        await release_classifier.wait()
        return ConversationDecision.CHAT

    async def scenario() -> None:
        with FakeILink() as fake:
            fake.updates = [
                {"ret": 0, "msgs": [_msg("slow task", mid=1)], "get_updates_buf": "c1"}
            ]
            stop = asyncio.Event()
            service = asyncio.create_task(
                serve_wechat(
                    config=_config(fake, workspace, state_dir, max_polls=20),
                    client=_client(fake),
                    state=WechatState(state_dir),
                    task_executor=execute,
                    conversation_classifier=classifier,
                    stop_event=stop,
                    allow_insecure_local_http=True,
                )
            )
            try:
                await asyncio.wait_for(worker_started.wait(), timeout=3)
                fake.updates.append(
                    {
                        "ret": 0,
                        "msgs": [_msg("进度怎么样", mid=3)],
                        "get_updates_buf": "c2",
                    }
                )
                await asyncio.wait_for(classifier_started.wait(), timeout=3)
                fake.updates.append(
                    {
                        "ret": 0,
                        "msgs": [_msg("/status", mid=2)],
                        "get_updates_buf": "c3",
                    }
                )
                for _ in range(100):
                    if any(
                        "任务正在执行" in str(m.get("item_list")) for m in fake.sent
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert any("任务正在执行" in str(m.get("item_list")) for m in fake.sent)
                assert not release_classifier.is_set()
            finally:
                release_classifier.set()
                stop.set()
                await asyncio.wait_for(service, timeout=5)

    asyncio.run(scenario())


def test_action_question_with_classifier_task_goes_to_queue_not_chat(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    chats: list[str] = []
    calls: list[str] = []

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    async def chat(question: str, sender: str, snapshot: str) -> str:
        chats.append(question)
        return "不应调用"

    async def classifier(text: str) -> ConversationDecision:
        return ConversationDecision.TASK

    async def scenario() -> None:
        with FakeILink() as fake:
            fake.updates = [
                {
                    "ret": 0,
                    "msgs": [_msg("你能帮我修复这个问题\uff1f", mid=3)],
                    "get_updates_buf": "c1",
                }
            ]
            await serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=20),
                client=_client(fake),
                state=WechatState(state_dir),
                task_executor=execute,
                conversation_executor=chat,
                conversation_classifier=classifier,
                allow_insecure_local_http=True,
            )

    asyncio.run(scenario())
    assert chats == [] and calls == ["你能帮我修复这个问题\uff1f"]


def test_restart_pending_chat_answers_and_sending_unknown_does_not_replay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state = WechatState(state_dir)
    state.record_received(
        "msg:chat", sender="u1", context_token="ctx", text="进度怎么样"
    )
    state.mark("msg:chat", "chat_pending", sender="u1", context_token="ctx")
    state.record_received(
        "msg:sent", sender="u1", context_token="ctx2", text="进度怎么样"
    )
    state.mark("msg:sent", "chat_sending", sender="u1", context_token="ctx2")
    chats: list[str] = []

    async def chat(question: str, sender: str, snapshot: str) -> str:
        chats.append(question)
        return "恢复后的回答"

    async def classifier(text: str) -> ConversationDecision:
        return ConversationDecision.CHAT

    async def scenario() -> None:
        with FakeILink() as fake:
            await serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=20),
                client=_client(fake),
                state=state,
                task_executor=_completed(workspace),
                conversation_executor=chat,
                conversation_classifier=classifier,
                allow_insecure_local_http=True,
            )
            replies = "\n".join(str(m.get("item_list")) for m in fake.sent)
            assert "恢复后的回答" in replies

    asyncio.run(scenario())
    assert chats == ["进度怎么样"]
    assert (WechatState(state_dir).get_record("msg:sent") or {}).get(
        "state"
    ) == "chat_delivery_unknown"


def test_parse_conversation_decision_is_strict() -> None:
    from cairn.integrations.wechat.serve import (
        ConversationDecision,
        parse_conversation_decision,
    )

    assert parse_conversation_decision("CHAT") is ConversationDecision.CHAT
    assert parse_conversation_decision('"TASK"') is ConversationDecision.TASK
    assert parse_conversation_decision("maybe") is ConversationDecision.UNKNOWN
    assert parse_conversation_decision(None) is ConversationDecision.UNKNOWN


def test_unknown_clarification_persists_and_consumes_reply_once(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    release = asyncio.Event()
    chats: list[str] = []

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        await release.wait()
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    async def chat(question: str, sender: str, snapshot: str) -> str:
        chats.append(question)
        return "已查询到进度。"

    async def classifier(text: str) -> ConversationDecision:
        if "slow task" in text:
            return ConversationDecision.TASK
        return ConversationDecision.UNKNOWN

    async def scenario() -> None:
        with FakeILink() as fake:
            fake.updates = [
                {
                    "ret": 0,
                    "msgs": [_msg("slow task", mid=1), _msg("那个东西呢", mid=2)],
                    "get_updates_buf": "c1",
                }
            ]
            stop = asyncio.Event()
            service = asyncio.create_task(
                serve_wechat(
                    config=_config(fake, workspace, state_dir, max_polls=30),
                    client=_client(fake),
                    state=WechatState(state_dir),
                    task_executor=execute,
                    conversation_executor=chat,
                    conversation_classifier=classifier,
                    stop_event=stop,
                    allow_insecure_local_http=True,
                )
            )
            try:
                for _ in range(100):
                    if any("意图不明确" in str(m.get("item_list")) for m in fake.sent):
                        break
                    await asyncio.sleep(0.01)
                assert any("意图不明确" in str(m.get("item_list")) for m in fake.sent)
                fake.updates.append(
                    {
                        "ret": 0,
                        "msgs": [_msg("查询进度", mid=3)],
                        "get_updates_buf": "c2",
                    }
                )
                for _ in range(100):
                    if any(
                        "已查询到进度" in str(m.get("item_list")) for m in fake.sent
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert any("已查询到进度" in str(m.get("item_list")) for m in fake.sent)
                # Duplicate reply ID is already consumed and must not create work.
                fake.updates.append(
                    {
                        "ret": 0,
                        "msgs": [_msg("安排新任务", mid=3)],
                        "get_updates_buf": "c3",
                    }
                )
                await asyncio.sleep(0.05)
                record = WechatState(state_dir).get_record("msg:3")
                assert (
                    record is not None and record["state"] == "clarification_consumed"
                )
            finally:
                release.set()
                stop.set()
                await asyncio.wait_for(service, timeout=5)

    asyncio.run(scenario())
    assert chats and chats[0] == "那个东西呢"


def test_classifier_provider_error_keeps_service_alive(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    release = asyncio.Event()

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        await release.wait()
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    async def classifier(text: str) -> ConversationDecision:
        if "slow task" in text:
            return ConversationDecision.TASK
        raise RuntimeError("provider-secret-ABC")

    async def scenario() -> None:
        with FakeILink() as fake:
            fake.updates = [
                {
                    "ret": 0,
                    "msgs": [_msg("slow task", mid=1), _msg("进度怎么样", mid=2)],
                    "get_updates_buf": "c1",
                }
            ]
            stop = asyncio.Event()
            service = asyncio.create_task(
                serve_wechat(
                    config=_config(fake, workspace, state_dir, max_polls=30),
                    client=_client(fake),
                    state=WechatState(state_dir),
                    task_executor=execute,
                    conversation_classifier=classifier,
                    stop_event=stop,
                    allow_insecure_local_http=True,
                )
            )
            try:
                for _ in range(100):
                    if any(
                        "provider-secret" in str(m.get("item_list")) for m in fake.sent
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert any(
                    "provider-secret" in str(m.get("item_list")) for m in fake.sent
                )
                fake.updates.append(
                    {
                        "ret": 0,
                        "msgs": [_msg("/status", mid=3)],
                        "get_updates_buf": "c2",
                    }
                )
                for _ in range(100):
                    if any(
                        "任务正在执行" in str(m.get("item_list")) for m in fake.sent
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert any("任务正在执行" in str(m.get("item_list")) for m in fake.sent)
            finally:
                release.set()
                stop.set()
                await asyncio.wait_for(service, timeout=5)

    asyncio.run(scenario())


def test_conversation_text_returns_structured_empty_and_error() -> None:
    completed = HeadlessResult(
        task_id="chat",
        status=HeadlessStatus.COMPLETED,
        workspace="/tmp",
        final_response="hi",
    )
    assert conversation_text(completed) == "hi"
    with pytest.raises(ConversationExecutionError) as empty:
        conversation_text(
            HeadlessResult(
                task_id="chat",
                status=HeadlessStatus.COMPLETED,
                workspace="/tmp",
                final_response="  ",
            )
        )
    assert empty.value.category == "empty_response"
    with pytest.raises(ConversationExecutionError) as failed:
        conversation_text(
            HeadlessResult(
                task_id="chat",
                status=HeadlessStatus.RUNTIME_ERROR,
                workspace="/tmp",
                error="provider unavailable",
            )
        )
    assert failed.value.category == "runtime_error"


def test_close_conversation_sessions_closes_all_and_reports_error() -> None:
    class Session:
        def __init__(self, fail: bool = False) -> None:
            self.closed = 0
            self.fail = fail

        def close(self) -> None:
            self.closed += 1
            if self.fail:
                raise RuntimeError("close failed")

    first = Session()
    second = Session(fail=True)
    error = close_conversation_sessions(cast(list[DurableSession], [first, second]))
    assert first.closed == 1 and second.closed == 1
    assert isinstance(error, RuntimeError)


def test_default_conversation_executor_closes_durable_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.integrations.wechat import serve as serve_module

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CAIRN_AGENT_HOME", str(tmp_path / "agent"))
    config = ServeConfig(
        workspace=workspace,
        state_dir=state_dir,
        account=WechatAccount(
            bot_token="t", ilink_bot_id="b", base_url="http://127.0.0.1"
        ),
        approval=None,
        allowed_senders=frozenset({"u1"}),
    )

    closed: list[bool] = []

    class Session:
        def close(self) -> None:
            closed.append(True)

    def factory(sender: str, active: list[object], *, purpose: str = "task") -> object:
        def build(agent: object) -> object:
            session = Session()
            active.append(session)
            return session

        return build

    async def fake_run(**kwargs: object) -> HeadlessResult:
        kwargs["durable_session_factory"](object())  # type: ignore[operator]
        return HeadlessResult(
            task_id="chat",
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
            final_response="ok",
        )

    monkeypatch.setattr(serve_module, "_run_via_config_async", fake_run)
    executor = default_conversation_executor(config, factory)
    call = cast(Callable[[str, str, str], Coroutine[Any, Any, str]], executor)
    assert asyncio.run(call("hello", "u1", "snap")) == "ok"
    assert closed == [True]


def test_task_holds_lease_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CAIRN_AGENT_HOME", str(tmp_path / "agent"))
    config = ServeConfig(
        workspace=workspace,
        state_dir=state_dir,
        account=WechatAccount(
            bot_token="t", ilink_bot_id="b", base_url="http://127.0.0.1"
        ),
        approval=None,
        allowed_senders=frozenset({"u1"}),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    class Tool:
        name = "write_file"
        description = "write"

        def schema(self) -> dict[str, object]:
            return {"name": self.name}

        def validate(self, arguments: dict[str, object]) -> None:
            return None

        async def execute(
            self,
            arguments: dict[str, object],
            *,
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            raise AssertionError("should not execute")

    async def fake_run(**kwargs: object) -> HeadlessResult:
        started.set()
        await release.wait()
        return HeadlessResult(
            task_id=kwargs["task_id"],  # type: ignore[arg-type]
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
            final_response="done",
        )

    monkeypatch.setattr(
        "cairn.integrations.wechat.serve._run_via_config_async", fake_run
    )

    async def scenario() -> None:
        executor = default_task_executor(config)
        call = cast(
            Callable[[str, asyncio.Event, str], Coroutine[Any, Any, HeadlessResult]],
            executor,
        )
        task = asyncio.create_task(call("work", asyncio.Event(), "t1"))
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            lease = executor.project_lease  # type: ignore[attr-defined]
            assert lease.acquire() is False
            result = await LeaseTool(Tool(), lease).execute({})
            assert result.error_type == "project_busy"
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=2)
        assert lease.acquire() is True
        lease.release()

    asyncio.run(scenario())


def test_default_conversation_executor_reports_empty_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.integrations.wechat import serve as serve_module

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setenv("CAIRN_AGENT_HOME", str(tmp_path / "agent"))
    config = ServeConfig(
        workspace=workspace,
        state_dir=state_dir,
        account=WechatAccount(
            bot_token="t", ilink_bot_id="b", base_url="http://127.0.0.1"
        ),
        approval=None,
        allowed_senders=frozenset({"u1"}),
    )

    def factory(sender: str, active: list[object], *, purpose: str = "task") -> object:
        def build(agent: object) -> object:
            return object()

        return build

    async def fake_run(**kwargs: object) -> HeadlessResult:
        return HeadlessResult(
            task_id="chat",
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
            final_response="",
        )

    monkeypatch.setattr(serve_module, "_run_via_config_async", fake_run)
    executor = default_conversation_executor(config, factory)
    call = cast(Callable[[str, str, str], Coroutine[Any, Any, str]], executor)
    with pytest.raises(ConversationExecutionError) as exc:
        asyncio.run(call("hello", "u1", "snap"))
    assert exc.value.category == "empty_response"
