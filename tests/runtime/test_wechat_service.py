"""Offline WeChat iLink closed-loop regressions: no real WeChat or paid model."""

import asyncio
import json
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, ToolCall
from cairn.infra.wechat.ilink import (
    ILinkClient,
    ILinkError,
    redirect_base_url,
    validate_base_url,
)
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
    ProviderApprovalMismatch,
    ProviderApprovalRequired,
    _run_via_config_async,
    run_headless_task,
)
from cairn.terminal.wechat.login import QRLoginError, run_login
from cairn.terminal.wechat.serve import ConversationDecision, ServeConfig, serve_wechat
from tests.support.fake_ilink import FakeILink
from tests.support.runtime import SequenceLLM

TOKEN = "secret-bot-token-do-not-leak"


def _account(fake: FakeILink) -> WechatAccount:
    return WechatAccount(
        bot_token=TOKEN,
        ilink_bot_id="bot-1",
        base_url=fake.url,
        ilink_user_id="owner",
    )


def _msg(
    text: str | None,
    *,
    sender: str = "u1",
    context: str = "ctx-1",
    mid: int | None = 1,
    item_type: int = 1,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_type": 1,
        "from_user_id": sender,
        "context_token": context,
        "message_id": mid,
        "item_list": [
            {"type": item_type, "text_item": {"text": text or ""}}
            if item_type == 1
            else {"type": item_type}
        ],
    }
    if mid is None:
        message.pop("message_id")
    return message


def _config(
    fake: FakeILink,
    workspace: Path,
    state_dir: Path,
    *,
    max_polls: int | None = 1,
) -> ServeConfig:
    return ServeConfig(
        workspace=workspace,
        state_dir=state_dir,
        account=_account(fake),
        approval=None,
        allowed_senders=frozenset({"u1"}),
        max_polls=max_polls,
        poll_interval=0.0,
    )


def _client(fake: FakeILink) -> ILinkClient:
    return ILinkClient(base_url=fake.url, bot_token=TOKEN, allow_insecure_local=True)


def _completed(
    workspace: Path,
) -> Callable[[str, asyncio.Event, str], Awaitable[HeadlessResult]]:
    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            verified=True,
            workspace=str(workspace),
            final_response="done: " + prompt,
        )

    return executor


async def _noop(_delay: float) -> None:
    return None


def test_login_saves_account_with_secure_state(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    with FakeILink() as fake:
        fake.qr_statuses = [
            {"status": "wait"},
            {"status": "scaned"},
            {"status": "need_verifycode"},
            {"status": "confirmed"},
        ]
        fake.confirm = {
            "bot_token": TOKEN,
            "ilink_bot_id": "bot-1",
            "baseurl": fake.url,
            "ilink_user_id": "owner",
        }
        account = asyncio.run(
            run_login(
                state_dir=state_dir,
                workspace=workspace,
                base_url=fake.url,
                prompt_verify=lambda _m: "1234",
                sleep=_noop,
                interval=0.0,
                max_polls=10,
                allow_insecure_local_base=True,
            )
        )
        assert account.bot_token == TOKEN
        assert WechatState(state_dir).load_account() == account
        assert stat.S_IMODE((state_dir / "account.json").stat().st_mode) == 0o600
        assert fake.qr_posts == 1
        assert any("bot_type=3" in request["path"] for request in fake.requests)
        assert fake.verify_codes == ["1234"]
        assert not any(TOKEN in str(request["body"]) for request in fake.requests)


def test_login_refuses_state_dir_inside_workspace_before_token_save(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with FakeILink() as fake, pytest.raises(ValueError):
        asyncio.run(
            run_login(
                state_dir=workspace / "state",
                workspace=workspace,
                base_url=fake.url,
                prompt_verify=lambda _m: "1",
                sleep=_noop,
                interval=0.0,
                max_polls=1,
                allow_insecure_local_base=True,
            )
        )
    assert not (workspace / "state" / "account.json").exists()


def test_login_rejects_insecure_confirmed_base_url(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with FakeILink() as fake:
        fake.qr_statuses = [{"status": "confirmed"}]
        fake.confirm = {
            "bot_token": TOKEN,
            "ilink_bot_id": "bot-1",
            "baseurl": "http://evil.example",
        }
        with pytest.raises(QRLoginError):
            asyncio.run(
                run_login(
                    state_dir=tmp_path.parent / (tmp_path.name + "-state"),
                    workspace=workspace,
                    base_url=fake.url,
                    sleep=_noop,
                    interval=0.0,
                    max_polls=2,
                    allow_insecure_local_base=True,
                )
            )


def test_base_url_validation_and_redirect() -> None:
    assert validate_base_url("https://example.test") == "https://example.test"
    assert redirect_base_url("example.test") == "https://example.test"
    with pytest.raises(ILinkError):
        redirect_base_url("http://example.test")
    with pytest.raises(ILinkError):
        validate_base_url("https://user:pass@example.test")
    with pytest.raises(ILinkError):
        validate_base_url("http://127.0.0.1:9")  # loopback needs explicit test flag
    assert (
        validate_base_url("http://127.0.0.1:9", allow_insecure_local=True)
        == "http://127.0.0.1:9"
    )


def test_state_existing_corruption_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "state.json").write_text(
        json.dumps({"version": 1, "cursor": "", "messages": []}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        WechatState(root).load()
    (root / "state.json").write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(ValueError):
        WechatState(root).load()


def test_missing_state_file_initialises_fresh(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "state")
    assert state.get_cursor() == ""
    assert state.records_in_state("received") == []


def test_login_then_first_serve_start(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    with FakeILink() as fake:
        fake.qr_statuses = [{"status": "confirmed"}]
        fake.confirm = {
            "bot_token": TOKEN,
            "ilink_bot_id": "bot-1",
            "baseurl": fake.url,
        }
        account = asyncio.run(
            run_login(
                state_dir=state_dir,
                workspace=workspace,
                base_url=fake.url,
                sleep=_noop,
                interval=0.0,
                max_polls=2,
                allow_insecure_local_base=True,
            )
        )
        config = ServeConfig(
            workspace=workspace,
            state_dir=state_dir,
            account=account,
            approval=None,
            allowed_senders=frozenset({"u1"}),
            max_polls=1,
        )
        report = asyncio.run(
            serve_wechat(
                config=config,
                client=_client(fake),
                state=WechatState(state_dir),
                task_executor=_completed(workspace),
                allow_insecure_local_http=True,
            )
        )
        assert report.polls == 1


def test_serve_runs_trusted_text_and_dedupes_redelivery(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    calls: list[str] = []

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            verified=True,
            workspace=str(workspace),
            final_response="done",
        )

    with FakeILink() as fake:
        batch = {
            "ret": 0,
            "msgs": [_msg("do it", mid=1, context="ctx-1")],
            "get_updates_buf": "cur-1",
        }
        fake.updates = [batch, {**batch, "get_updates_buf": "cur-2"}]
        report = asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=50),
                client=_client(fake),
                state=WechatState(state_dir),
                task_executor=executor,
                allow_insecure_local_http=True,
            )
        )
        assert calls == ["do it"]
        assert report.tasks_started == 1
        assert WechatState(state_dir).get_cursor() == "cur-2"
        replies = "\n".join(str(msg.get("item_list")) for msg in fake.sent)
        assert "已收到" in str(fake.sent[0].get("item_list"))
        assert "do it" not in str(fake.sent[0].get("item_list"))
        assert replies.count("已收到") == 1
        assert "done" in replies and "completed" in replies
        assert TOKEN not in replies
        assert [call.get("status") for call in fake.typing_calls] == [1, 2]


def test_serve_status_busy_and_cancel(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    calls: list[str] = []

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


def test_serve_rejects_missing_id_untrusted_and_non_text(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
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
        fake.updates = [
            {
                "ret": 0,
                "msgs": [
                    _msg("no id", mid=None),
                    _msg("stranger", mid=2, sender="stranger"),
                    _msg("image only", mid=3, item_type=2),
                ],
                "get_updates_buf": "cur",
            }
        ]
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=2),
                client=_client(fake),
                state=WechatState(state_dir),
                task_executor=executor,
                allow_insecure_local_http=True,
            )
        )
        assert calls == []
        replies = "\n".join(str(msg.get("item_list")) for msg in fake.sent)
        assert "缺少稳定 ID" in replies
        assert "仅支持文本" in replies
        assert "stranger" not in replies


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


def test_serve_refuses_state_inside_workspace_before_any_state(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with FakeILink() as fake, pytest.raises(ValueError):
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, workspace / "state", max_polls=1),
                client=_client(fake),
                state=WechatState(workspace / "state"),
                task_executor=_completed(workspace),
                allow_insecure_local_http=True,
            )
        )
    assert not (workspace / "state").exists()


def test_real_offline_task_chain_edits_workspace_and_replies_same_context(
    tmp_path: Path,
) -> None:
    """Fake iLink -> real run_headless_task/run_turn + SequenceLLM -> edit_file."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    note = workspace / "note.txt"
    note.write_text("bad\n", encoding="utf-8")
    state_dir = tmp_path / "state"

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        llm = SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="edit-1",
                            name="edit_file",
                            arguments={
                                "path": "note.txt",
                                "old_text": "bad\n",
                                "new_text": "good\n",
                            },
                        )
                    ]
                ),
                LLMResponse(content="task finished"),
            ]
        )
        return await run_headless_task(
            workspace=workspace,
            prompt=prompt,
            grant_workspace_trust=True,
            grant_shell=False,
            llm=llm,
            budget=RunBudget(max_steps=10),
            task_id=task_id,
            cancellation_event=cancel,
        )

    with FakeILink() as fake:
        fake.updates = [
            {
                "ret": 0,
                "msgs": [_msg("fix note", mid=11, context="ctx-e2e")],
                "get_updates_buf": "cur-e2e",
            }
        ]
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir, max_polls=50),
                client=_client(fake),
                state=WechatState(state_dir),
                task_executor=executor,
                allow_insecure_local_http=True,
            )
        )
    assert note.read_text(encoding="utf-8") == "good\n"
    assert fake.sent, "expected a reply for the same conversation"
    assert fake.sent[0].get("context_token") == "ctx-e2e"
    all_replies = "\n".join(str(msg.get("item_list")) for msg in fake.sent)
    assert "completed" in all_replies
    assert "task finished" in all_replies


def test_default_provider_approval_is_rejected_before_credentials_or_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".cairn").mkdir(parents=True)
    (workspace / ".cairn" / "models.toml").write_text(
        'base_url = "https://x.test/v1"\napi_key_env = "K"\n'
        '\n[[models]]\nname = "primary"\nmodel_ids = ["m"]\n',
        encoding="utf-8",
    )
    called: list[str] = []

    def fail_key(*_a: object, **_k: object) -> str:
        called.append("key")
        raise AssertionError("credential must not be resolved before approval")

    def fail_runtime(*_a: object, **_k: object) -> object:
        called.append("model")
        raise AssertionError("model runtime must not be built before approval")

    monkeypatch.setattr("cairn.terminal.headless.resolve_provider_api_key", fail_key)
    monkeypatch.setattr("cairn.terminal.headless.build_provider_runtime", fail_runtime)

    with pytest.raises(ProviderApprovalRequired):
        asyncio.run(
            _run_via_config_async(
                workspace=workspace,
                prompt="x",
                task_id=None,
                grant_shell=False,
                approval=None,
                max_steps=2,
                trace_dir=tmp_path / "traces",
            )
        )
    assert called == []

    wrong = ProviderApproval(
        name="default",
        endpoint="https://wrong.test/v1",
        group="primary",
        key_env="K",
        model_ids=("m",),
    )
    with pytest.raises(ProviderApprovalMismatch):
        asyncio.run(
            _run_via_config_async(
                workspace=workspace,
                prompt="x",
                task_id=None,
                grant_shell=False,
                approval=wrong,
                max_steps=2,
                trace_dir=tmp_path / "traces",
            )
        )
    assert called == []


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


def test_runtime_build_failure_after_approval_can_redact_resolved_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".cairn").mkdir(parents=True)
    (workspace / ".cairn" / "models.toml").write_text(
        'base_url = "https://x.test/v1"\napi_key_env = "FAKE_KEY"\n'
        '\n[[models]]\nname = "primary"\nmodel_ids = ["m"]\n',
        encoding="utf-8",
    )
    captured: list[tuple[str, ...]] = []
    approval = ProviderApproval(
        name="default",
        endpoint="https://x.test/v1",
        group="primary",
        key_env="FAKE_KEY",
        model_ids=("m",),
    )
    monkeypatch.setenv("FAKE_KEY", "fake-provider-key")
    monkeypatch.setattr(
        "cairn.terminal.headless.resolve_provider_api_key",
        lambda *_a, **_k: "fake-provider-key",
    )

    def fail_runtime(*_a: object, **_k: object) -> object:
        raise RuntimeError("runtime failed with fake-provider-key")

    monkeypatch.setattr("cairn.terminal.headless.build_provider_runtime", fail_runtime)
    with pytest.raises(RuntimeError, match="fake-provider-key"):
        asyncio.run(
            _run_via_config_async(
                workspace=workspace,
                prompt="x",
                task_id=None,
                grant_shell=False,
                approval=approval,
                max_steps=2,
                trace_dir=tmp_path / "traces",
                on_secrets_resolved=lambda values: captured.append(tuple(values)),
            )
        )
    assert captured and "fake-provider-key" in captured[0]


def test_ack_failure_does_not_lose_task_or_claim_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.terminal.wechat.serve import ServeReport, _Inbound, _Service

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = WechatState(tmp_path / "state")
    calls: list[str] = []

    class FailingClient:
        async def send_text(self, **_kwargs: object) -> dict[str, object]:
            raise ILinkError("send failed")

        async def send_typing(self, **_kwargs: object) -> dict[str, object]:
            return {}

        async def get_config(self, **_kwargs: object) -> dict[str, object]:
            return {"typing_ticket": "t"}

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    async def scenario() -> None:
        service = _Service(
            config=type("C", (), {"redact_text": None})(),
            state=state,
            client=FailingClient(),  # type: ignore[arg-type]
            executor=executor,
            report=ServeReport(),
            stop_event=None,
        )
        service._start_task(
            "msg:1",
            _Inbound(
                key="msg:1",
                sender="u1",
                context_token="ctx",
                text="do it",
                stable=True,
            ),
        )
        assert service._current is not None
        await asyncio.wait_for(service._current, timeout=3)
        record = state.get_record("msg:1")
        assert record is not None and record["state"] == "completed"
        assert record.get("ack_attempted") is True
        assert record.get("ack_sent") is not True

    asyncio.run(scenario())
    assert calls == ["do it"]


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


def test_ack_never_echoes_input_before_provider_secrets_are_resolved(
    tmp_path: Path,
) -> None:
    from cairn.terminal.wechat.serve import ServeReport, _Inbound, _Service

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = WechatState(tmp_path / "state")
    sent: list[str] = []
    synthetic = "sk-synthetic-secret-" + "x" * 70

    class Client:
        async def send_text(self, **kwargs: object) -> dict[str, object]:
            sent.append(str(kwargs.get("text")))
            return {}

        async def send_typing(self, **_kwargs: object) -> dict[str, object]:
            return {}

        async def get_config(self, **_kwargs: object) -> dict[str, object]:
            return {"typing_ticket": "t"}

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

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
            "msg:secret",
            _Inbound(
                key="msg:secret",
                sender="u1",
                context_token="ctx",
                text="prefix " + synthetic,
                stable=True,
            ),
        )
        assert service._current is not None
        await asyncio.wait_for(service._current, timeout=3)

    asyncio.run(scenario())
    assert sent and "已收到" in sent[0]
    assert synthetic not in sent[0]
    assert "sk-synthetic" not in sent[0]


def test_lost_ack_response_is_not_retried(tmp_path: Path) -> None:
    from cairn.terminal.wechat.serve import ServeReport, _Inbound, _Service

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = WechatState(tmp_path / "state")
    ack_calls = 0

    class LostAckClient:
        async def send_text(self, **kwargs: object) -> dict[str, object]:
            nonlocal ack_calls
            if "已收到" in str(kwargs.get("text")):
                ack_calls += 1
                raise ILinkError("response lost after send")
            return {}

        async def send_typing(self, **_kwargs: object) -> dict[str, object]:
            return {}

        async def get_config(self, **_kwargs: object) -> dict[str, object]:
            return {"typing_ticket": "t"}

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    async def scenario() -> None:
        service = _Service(
            config=type("C", (), {"redact_text": None})(),
            state=state,
            client=LostAckClient(),  # type: ignore[arg-type]
            executor=executor,
            report=ServeReport(),
            stop_event=None,
        )
        service._start_task(
            "msg:3",
            _Inbound(
                key="msg:3",
                sender="u1",
                context_token="ctx",
                text="do it",
                stable=True,
            ),
        )
        assert service._current is not None
        await asyncio.wait_for(service._current, timeout=3)
        record = state.get_record("msg:3")
        assert record is not None and record["state"] == "completed"
        assert record.get("ack_attempted") is True
        assert record.get("ack_state") == "unknown"

    asyncio.run(scenario())
    assert ack_calls == 1


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


def test_default_wechat_executor_exposes_shared_task_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.infra.wechat.state import WechatAccount
    from cairn.terminal.wechat.serve import ServeConfig, default_task_executor

    captured: list[object] = []

    async def fake_run(**kwargs: object) -> HeadlessResult:
        captured.extend(kwargs.get("extra_tools", ()))  # type: ignore[arg-type]
        return HeadlessResult(
            task_id="t", status=HeadlessStatus.COMPLETED, workspace=str(tmp_path)
        )

    monkeypatch.setattr("cairn.terminal.wechat.serve._run_via_config_async", fake_run)
    safe_root = tmp_path.parent / (tmp_path.name + "-tasks-safe")
    monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(safe_root))
    config = ServeConfig(
        workspace=tmp_path,
        state_dir=tmp_path.parent / (tmp_path.name + "-state"),
        account=WechatAccount(
            bot_token="bot-token-very-long-123",
            ilink_bot_id="bot",
            base_url="http://127.0.0.1",
        ),
        approval=None,
        allowed_senders=frozenset({"u1"}),
    )
    executor = default_task_executor(config)

    async def run_it() -> None:
        await executor("hello", asyncio.Event(), "t")

    asyncio.run(run_it())
    names = {getattr(tool, "name", "") for tool in captured}
    assert {
        "task_create",
        "task_list",
        "task_detail",
        "task_update",
        "task_todo",
    } <= names


def test_default_wechat_executor_rejects_task_board_inside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.infra.wechat.state import WechatAccount
    from cairn.terminal.wechat.serve import ServeConfig, default_task_executor

    config = ServeConfig(
        workspace=tmp_path,
        state_dir=tmp_path.parent / (tmp_path.name + "-state"),
        account=WechatAccount(
            bot_token="bot-token-very-long-123",
            ilink_bot_id="bot",
            base_url="http://127.0.0.1",
        ),
        approval=None,
        allowed_senders=frozenset({"u1"}),
    )
    monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(tmp_path / "tasks"))
    with pytest.raises(ValueError):
        default_task_executor(config)

    link = tmp_path.parent / (tmp_path.name + "-tasks-link")
    link.symlink_to(tmp_path / "tasks", target_is_directory=True)
    monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(link))
    with pytest.raises(ValueError):
        default_task_executor(config)


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
                await serve_wechat(config=config, allow_insecure_local_http=True)

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


@pytest.mark.parametrize(
    ("variable", "through_symlink"),
    [
        ("TMPDIR", False),
        ("TMPDIR", True),
        ("UV_CACHE_DIR", False),
        ("UV_CACHE_DIR", True),
    ],
)
def test_wechat_session_refuses_agent_writable_storage(
    monkeypatch: pytest.MonkeyPatch, variable: str, through_symlink: bool
) -> None:
    from tempfile import TemporaryDirectory

    from cairn.terminal.wechat.serve import default_task_executor
    from tests.support.git import initialize_repository

    with TemporaryDirectory(
        prefix="cairn-wechat-writable-", dir=Path.home()
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        writable = root / "writable"
        writable.mkdir()
        state_parent = writable
        if through_symlink:
            state_parent = root / "alias"
            state_parent.symlink_to(writable, target_is_directory=True)
        monkeypatch.setenv(variable, str(writable))
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(root / "board"))
        llm = SequenceLLM([LLMResponse(content="should not run")])
        config = ServeConfig(
            workspace=workspace,
            state_dir=state_parent / "state",
            account=WechatAccount(
                bot_token="fake-bot-token-very-long",
                ilink_bot_id="bot",
                base_url="https://example.test",
            ),
            approval=None,
            allowed_senders=frozenset({"u1"}),
            max_steps=20,
        )

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
            return await execute("hello", asyncio.Event(), "first", sender="u1")  # type: ignore[call-arg]

        result = asyncio.run(flow())
        assert result.status is HeadlessStatus.RUNTIME_ERROR
        assert llm.calls == []
        assert not (writable / "state" / "sessions").exists()


def test_wechat_workspace_observation_uses_current_secret_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tempfile import TemporaryDirectory

    from cairn.infra.sessions.host import observe_workspace
    from cairn.infra.sessions.models import WorkspaceBinding
    from cairn.infra.workspace.workspace import Workspace
    from cairn.terminal.wechat.serve import default_task_executor
    from tests.support.git import initialize_repository

    with TemporaryDirectory(
        prefix="cairn-wechat-credentials-", dir=Path.home()
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(root / "board"))
        llm = SequenceLLM([LLMResponse(content="done")])
        observed: list[frozenset[str]] = []
        current_keys = frozenset({"CAIRN_TEST_CURRENT_PROVIDER_SECRET"})

        def record_observation(
            target: Workspace, secret_env_keys: frozenset[str]
        ) -> WorkspaceBinding:
            observed.append(secret_env_keys)
            return observe_workspace(target, secret_env_keys)

        monkeypatch.setattr(
            "cairn.terminal.wechat.serve.observe_workspace", record_observation
        )
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

        async def fake_run(**kwargs: object) -> HeadlessResult:
            return await run_headless_task(
                workspace=workspace,
                prompt=str(kwargs["prompt"]),
                task_id=str(kwargs["task_id"]),
                grant_workspace_trust=True,
                grant_shell=False,
                llm=llm,
                budget=RunBudget(max_steps=20),
                secret_env_keys=current_keys,
                durable_session_factory=kwargs["durable_session_factory"],  # type: ignore[arg-type]
            )

        monkeypatch.setattr(
            "cairn.terminal.wechat.serve._run_via_config_async", fake_run
        )

        async def flow() -> HeadlessResult:
            execute = default_task_executor(config)
            return await execute("hello", asyncio.Event(), "first", sender="u1")  # type: ignore[call-arg]

        result = asyncio.run(flow())
        assert result.status is HeadlessStatus.COMPLETED
        assert observed and all(current_keys <= keys for keys in observed)


@pytest.mark.parametrize("capability", ["shell", "network", "mcp"])
@pytest.mark.parametrize("allowed", [True, False])
def test_wechat_durable_session_respects_explicit_capabilities(
    monkeypatch: pytest.MonkeyPatch, capability: str, allowed: bool
) -> None:
    from tempfile import TemporaryDirectory
    from typing import Any

    from cairn.core.models import ToolResult
    from cairn.core.permissions import CapabilityRequest, PermissionCapability
    from cairn.infra.tools.base import ToolExecutionContext
    from cairn.terminal.wechat.serve import default_task_executor
    from tests.support.git import initialize_repository

    with TemporaryDirectory(
        prefix="cairn-wechat-permissions-", dir=Path.home()
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(root / "board"))
        invoked: list[str] = []

        class CapabilityTool:
            name = "capability_probe"
            description = "A fake tool for testing explicit permission."
            requires_authorization = True

            def schema(self) -> dict[str, Any]:
                return {"name": self.name, "description": self.description}

            def validate(self, arguments: dict[str, Any]) -> None:
                assert arguments == {}

            def requested_capability(
                self, arguments: dict[str, Any]
            ) -> CapabilityRequest:
                selected = (
                    PermissionCapability.NETWORK
                    if capability == "network"
                    else PermissionCapability.MCP_TOOL
                )
                return CapabilityRequest(capability=selected)

            async def execute(
                self,
                arguments: dict[str, Any],
                *,
                context: ToolExecutionContext | None = None,
            ) -> ToolResult:
                invoked.append(capability)
                return ToolResult(stdout="completed", exit_code=0)

        tool = CapabilityTool()
        call = (
            ToolCall(
                id="capability",
                name="bash",
                arguments={"command": "printf ok > shell-marker"},
            )
            if capability == "shell"
            else ToolCall(id="capability", name=tool.name, arguments={})
        )
        llm = SequenceLLM(
            [LLMResponse(tool_calls=[call]), LLMResponse(content="finished")]
        )
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
            allow_shell=allowed if capability == "shell" else capability == "network",
            allow_network=allowed if capability == "network" else capability == "mcp",
            allow_mcp=allowed if capability == "mcp" else capability == "shell",
            max_steps=20,
        )

        async def fake_run(**kwargs: object) -> HeadlessResult:
            return await run_headless_task(
                workspace=workspace,
                prompt=str(kwargs["prompt"]),
                task_id=str(kwargs["task_id"]),
                grant_workspace_trust=True,
                grant_shell=bool(kwargs["grant_shell"]),
                grant_mcp=bool(kwargs["grant_mcp"]),
                grant_network=bool(kwargs["grant_network"]),
                llm=llm,
                budget=RunBudget(max_steps=20),
                extra_tools=(tool,),
                durable_session_factory=kwargs["durable_session_factory"],  # type: ignore[arg-type]
            )

        monkeypatch.setattr(
            "cairn.terminal.wechat.serve._run_via_config_async", fake_run
        )

        async def flow() -> HeadlessResult:
            execute = default_task_executor(config)
            return await execute("run the task", asyncio.Event(), "first", sender="u1")  # type: ignore[call-arg]

        result = asyncio.run(flow())
        assert result.status is HeadlessStatus.COMPLETED
        assert len(llm.calls) == 2
        executed = (
            (workspace / "shell-marker").exists()
            if capability == "shell"
            else bool(invoked)
        )
        assert executed is allowed
        assert result.permission_denied is not allowed


def test_wechat_durable_failed_check_can_repair_and_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tempfile import TemporaryDirectory

    from cairn.core.tasks import TaskCheckStatus
    from cairn.terminal.wechat.serve import default_task_executor
    from tests.support.git import initialize_repository

    with TemporaryDirectory(
        prefix="cairn-wechat-repair-", dir=Path.home()
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(root / "board"))
        llm = SequenceLLM(
            [
                LLMResponse(content="first answer"),
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="repair",
                            name="write_file",
                            arguments={"path": "repaired", "content": "ok"},
                        )
                    ]
                ),
                LLMResponse(content="fixed"),
                LLMResponse(content="later answer"),
            ]
        )
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
            allow_shell=True,
            check_command="test -f repaired",
        )

        async def fake_run(**kwargs: object) -> HeadlessResult:
            return await run_headless_task(
                workspace=workspace,
                prompt=str(kwargs["prompt"]),
                task_id=str(kwargs["task_id"]),
                grant_workspace_trust=True,
                grant_shell=bool(kwargs["grant_shell"]),
                llm=llm,
                budget=RunBudget(max_steps=20),
                check_command=str(kwargs["check_command"]),
                durable_session_factory=kwargs["durable_session_factory"],  # type: ignore[arg-type]
            )

        monkeypatch.setattr(
            "cairn.terminal.wechat.serve._run_via_config_async", fake_run
        )

        async def flow() -> tuple[HeadlessResult, HeadlessResult]:
            execute = default_task_executor(config)
            first = await execute(
                "make a file", asyncio.Event(), "task-one", sender="u1"
            )  # type: ignore[call-arg]
            later = await execute("continue", asyncio.Event(), "task-two", sender="u1")  # type: ignore[call-arg]
            return first, later

        result, followup = asyncio.run(flow())
        assert result.status is HeadlessStatus.COMPLETED, result.error
        assert result.task_id == "task-one"
        assert result.check_status is TaskCheckStatus.PASSED
        assert result.check_attempts == 2
        assert result.verified
        assert followup.status is HeadlessStatus.COMPLETED
        assert followup.check_status is TaskCheckStatus.PASSED
        assert len(llm.calls) == 4
        messages = llm.calls[3][0]
        assert any(
            message.role == "assistant"
            and any(call.id == "repair" for call in message.tool_calls)
            for message in messages
        )
        assert any(
            message.role == "tool" and message.tool_call_id == "repair"
            for message in messages
        )
        assert (workspace / "repaired").read_text() == "ok"


@pytest.mark.parametrize("primary_cancelled", [False, True])
def test_wechat_session_close_failure_retains_owner_and_primary(
    monkeypatch: pytest.MonkeyPatch, primary_cancelled: bool
) -> None:
    from tempfile import TemporaryDirectory

    from cairn.runtime.durable_session import DurableSession
    from cairn.terminal.wechat.serve import default_task_executor
    from tests.support.git import initialize_repository

    with TemporaryDirectory(prefix="cairn-wechat-close-", dir=Path.home()) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(root / "board"))
        llm = SequenceLLM(
            [LLMResponse(content="first answer"), LLMResponse(content="second answer")]
        )
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
        original_close = DurableSession.close
        failures = 0

        def fail_close_once(session: DurableSession) -> None:
            nonlocal failures
            if failures == 0:
                failures += 1
                raise OSError("private synthetic cleanup detail")
            original_close(session)

        monkeypatch.setattr(DurableSession, "close", fail_close_once)
        runs = 0

        async def fake_run(**kwargs: object) -> HeadlessResult:
            nonlocal runs
            runs += 1
            result = await run_headless_task(
                workspace=workspace,
                prompt=str(kwargs["prompt"]),
                task_id=str(kwargs["task_id"]),
                grant_workspace_trust=True,
                grant_shell=False,
                llm=llm,
                budget=RunBudget(max_steps=20),
                durable_session_factory=kwargs["durable_session_factory"],  # type: ignore[arg-type]
            )
            if primary_cancelled and runs == 1:
                raise asyncio.CancelledError("first cancellation")
            return result

        monkeypatch.setattr(
            "cairn.terminal.wechat.serve._run_via_config_async", fake_run
        )

        async def flow() -> HeadlessResult:
            execute = default_task_executor(config)
            if primary_cancelled:
                with pytest.raises(asyncio.CancelledError) as caught:
                    await execute("first", asyncio.Event(), "one", sender="u1")  # type: ignore[call-arg]
                assert caught.value.args == ("first cancellation",)
                notes = " ".join(getattr(caught.value, "__notes__", ()))
                assert "cleanup" in notes.lower()
                assert "private synthetic cleanup detail" not in notes
            else:
                with pytest.raises(OSError):
                    await execute("first", asyncio.Event(), "one", sender="u1")  # type: ignore[call-arg]
            return await execute("second", asyncio.Event(), "two", sender="u1")  # type: ignore[call-arg]

        result = asyncio.run(flow())
        assert result.status is HeadlessStatus.COMPLETED
        assert len(llm.calls) == 2
        assert any(
            message.role == "assistant" and message.content == "first answer"
            for message in llm.calls[1][0]
        )


def test_wechat_reopen_failure_releases_attached_session_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tempfile import TemporaryDirectory

    from cairn.runtime.durable_session import DurableSession
    from cairn.terminal.wechat.serve import default_task_executor
    from tests.support.git import initialize_repository

    with TemporaryDirectory(
        prefix="cairn-wechat-reopen-", dir=Path.home()
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(root / "board"))
        llm = SequenceLLM(
            [LLMResponse(content="first answer"), LLMResponse(content="second answer")]
        )
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
        original_inspect = DurableSession.inspect
        original_close = DurableSession.close
        close_calls = 0

        def fail_inspection(_session: DurableSession) -> object:
            raise OSError("synthetic inspection failure")

        def fail_close_once(session: DurableSession) -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 1:
                raise OSError("synthetic cleanup failure")
            original_close(session)

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

        async def flow() -> tuple[HeadlessResult, HeadlessResult, HeadlessResult]:
            execute = default_task_executor(config)
            first = await execute("first", asyncio.Event(), "one", sender="u1")  # type: ignore[call-arg]
            monkeypatch.setattr(DurableSession, "inspect", fail_inspection)
            monkeypatch.setattr(DurableSession, "close", fail_close_once)
            failed = await execute("second", asyncio.Event(), "two", sender="u1")  # type: ignore[call-arg]
            assert close_calls == 1
            monkeypatch.setattr(DurableSession, "inspect", original_inspect)
            later = await execute("later", asyncio.Event(), "three", sender="u1")  # type: ignore[call-arg]
            return first, failed, later

        first, failed, later = asyncio.run(flow())
        assert first.status is HeadlessStatus.COMPLETED
        assert failed.status is HeadlessStatus.RUNTIME_ERROR
        assert later.status is HeadlessStatus.COMPLETED
        assert len(llm.calls) == 2
        assert close_calls >= 2
        assert any(
            message.role == "assistant" and message.content == "first answer"
            for message in llm.calls[1][0]
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


def test_wechat_attachment_failure_retains_unclosed_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tempfile import TemporaryDirectory

    from cairn.infra.sessions.host import observe_workspace
    from cairn.infra.sessions.models import WorkspaceBinding
    from cairn.infra.sessions.store import SessionStore
    from cairn.infra.workspace.workspace import Workspace
    from cairn.terminal.wechat.serve import default_task_executor
    from tests.support.git import initialize_repository

    with TemporaryDirectory(
        prefix="cairn-wechat-attach-", dir=Path.home()
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(root / "board"))
        llm = SequenceLLM([LLMResponse(content="later answer")])
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
        observations = 0
        closes = 0
        original_close = SessionStore.close

        def fail_observation_once(
            target: Workspace, secret_env_keys: frozenset[str]
        ) -> WorkspaceBinding:
            nonlocal observations
            observations += 1
            if observations == 1:
                raise OSError("synthetic observation failure")
            return observe_workspace(target, secret_env_keys)

        def fail_close_once(store: SessionStore) -> None:
            nonlocal closes
            closes += 1
            if closes == 1:
                raise OSError("synthetic close failure")
            original_close(store)

        monkeypatch.setattr(
            "cairn.terminal.wechat.serve.observe_workspace", fail_observation_once
        )
        monkeypatch.setattr(SessionStore, "close", fail_close_once)

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

        async def flow() -> tuple[HeadlessResult, HeadlessResult]:
            execute = default_task_executor(config)
            failed = await execute("first", asyncio.Event(), "one", sender="u1")  # type: ignore[call-arg]
            later = await execute("later", asyncio.Event(), "two", sender="u1")  # type: ignore[call-arg]
            return failed, later

        failed, later = asyncio.run(flow())
        assert failed.status is HeadlessStatus.RUNTIME_ERROR
        assert "synthetic observation failure" in (failed.error or "")
        assert "synthetic close failure" not in (failed.error or "")
        assert later.status is HeadlessStatus.COMPLETED
        assert closes >= 2
        assert len(llm.calls) == 1


def test_wechat_tasks_link_to_their_persistent_sender_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tempfile import TemporaryDirectory
    from typing import Any

    from cairn.core.task_board import TaskSource
    from cairn.infra.sessions.store import SessionStore
    from cairn.infra.task_board.store import TaskBoardStore
    from tests.support.git import initialize_repository

    with TemporaryDirectory(
        prefix="cairn-wechat-task-session-", dir=Path.home()
    ) as root_text:
        root = Path(root_text)
        workspace = root / "workspace"
        workspace.mkdir()
        initialize_repository(workspace)
        board_dir = root / "board"
        state_dir = root / "state"
        monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
        monkeypatch.setenv("CAIRN_TASK_BOARD_DIR", str(board_dir))
        llm = SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="a1", name="task_create", arguments={"title": "A first"}
                        )
                    ]
                ),
                LLMResponse(content="A first done"),
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="b1", name="task_create", arguments={"title": "B first"}
                        )
                    ]
                ),
                LLMResponse(content="B first done"),
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="a2", name="task_create", arguments={"title": "A later"}
                        )
                    ]
                ),
                LLMResponse(content="A later done"),
            ]
        )

        async def fake_run(**kwargs: Any) -> HeadlessResult:
            return await run_headless_task(
                workspace=workspace,
                prompt=kwargs["prompt"],
                task_id=kwargs["task_id"],
                grant_workspace_trust=True,
                grant_shell=False,
                llm=llm,
                budget=RunBudget(max_steps=20),
                extra_tools=kwargs["extra_tools"],
                durable_session_factory=kwargs["durable_session_factory"],
            )

        monkeypatch.setattr(
            "cairn.terminal.wechat.serve._run_via_config_async", fake_run
        )
        with FakeILink() as fake:
            config = ServeConfig(
                workspace=workspace,
                state_dir=state_dir,
                account=_account(fake),
                approval=None,
                allowed_senders=frozenset({"u1", "u2"}),
                max_steps=20,
                max_polls=70,
                poll_interval=0.02,
            )

            async def exchange(
                sender: str, message: str, message_id: int, cursor: str
            ) -> None:
                fake.updates = [
                    {
                        "ret": 0,
                        "msgs": [_msg(message, sender=sender, mid=message_id)],
                        "get_updates_buf": cursor,
                    }
                ]
                await serve_wechat(config=config, allow_insecure_local_http=True)

            asyncio.run(exchange("u1", "A first request", 201, "first"))
            asyncio.run(exchange("u2", "B first request", 202, "second"))
            asyncio.run(exchange("u1", "A later request", 203, "third"))

        for message_id in (201, 202, 203):
            record = WechatState(state_dir).get_record(f"msg:{message_id}")
            assert record is not None and record["state"] == "completed"
        assert len(llm.calls) == 6
        b_context = llm.calls[2][0]
        a_context = llm.calls[4][0]
        assert not any(m.content == "A first request" for m in b_context)
        assert any(m.content == "A first request" for m in a_context)
        assert not any(m.content == "B first request" for m in a_context)

        tasks = {task.title: task for task in TaskBoardStore(board_dir).load()}
        assert set(tasks) == {"A first", "B first", "A later"}
        assert all(task.source is TaskSource.WECHAT for task in tasks.values())
        assert tasks["A first"].session_id == tasks["A later"].session_id
        assert tasks["A first"].session_id != tasks["B first"].session_id

        store = SessionStore(
            state_dir / "sessions", mode="inspect", forbidden_roots=(workspace,)
        )
        try:
            session_ids = {summary.session_id for summary in store.list_sessions()}
        finally:
            store.close()
        assert {
            tasks["A first"].session_id.removeprefix("wechat:"),
            tasks["B first"].session_id.removeprefix("wechat:"),
        } == session_ids


def test_serve_queues_busy_messages_in_received_order(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = WechatState(tmp_path / "state")
    calls: list[str] = []

    async def flow(fake: FakeILink) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        stop = asyncio.Event()

        async def execute(
            prompt: str, cancel: asyncio.Event, task_id: str
        ) -> HeadlessResult:
            calls.append(prompt)
            if prompt == "first":
                started.set()
                await release.wait()
            return HeadlessResult(
                task_id=task_id,
                status=HeadlessStatus.COMPLETED,
                workspace=str(workspace),
                final_response="done",
            )

        fake.updates = [
            {"ret": 0, "msgs": [_msg("first", mid=99)], "get_updates_buf": "c1"},
            {
                "ret": 0,
                "msgs": [_msg("A", mid=10), _msg("B", mid=2), _msg("C", mid=1)],
                "get_updates_buf": "c2",
            },
            {"ret": 0, "msgs": [_msg("A", mid=10)], "get_updates_buf": "c3"},
        ]
        config = _config(fake, workspace, state.root, max_polls=None)
        service = asyncio.create_task(
            serve_wechat(
                config=config,
                client=_client(fake),
                state=state,
                task_executor=execute,
                stop_event=stop,
                allow_insecure_local_http=True,
            )
        )

        async def wait_for_state(keys: tuple[str, ...], expected: str) -> None:
            while not all(
                (state.get_record(key) or {}).get("state") == expected for key in keys
            ):
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await asyncio.wait_for(
                wait_for_state(("msg:10", "msg:2", "msg:1"), "queued"), timeout=5
            )
            assert calls == ["first"]
            release.set()
            await asyncio.wait_for(
                wait_for_state(("msg:99", "msg:10", "msg:2", "msg:1"), "completed"),
                timeout=5,
            )
        finally:
            stop.set()
            release.set()
            await asyncio.wait_for(service, timeout=5)

    with FakeILink() as fake:
        asyncio.run(flow(fake))
        receipts = "\n".join(str(message.get("item_list")) for message in fake.sent)
        assert receipts.count("已收到") == 4
    assert calls == ["first", "A", "B", "C"]


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
    from cairn.terminal.wechat.serve import (
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


def test_sigterm_style_stop_wakes_slow_poll(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    stop = asyncio.Event()

    class SlowClient:
        def __init__(self) -> None:
            self.get_called = asyncio.Event()
            self.notify_stopped = False
            self.closed = False

        async def notify_start(self) -> None:
            return None

        async def notify_stop(self) -> None:
            self.notify_stopped = True

        async def close(self) -> None:
            self.closed = True

        async def get_updates(
            self, *args: object, **kwargs: object
        ) -> dict[str, object]:
            self.get_called.set()
            await asyncio.sleep(30)
            return {"ret": 0, "msgs": [], "get_updates_buf": "late"}

        async def send_text(self, **kwargs: object) -> dict[str, object]:
            return {}

        async def get_config(self, **kwargs: object) -> dict[str, object]:
            return {}

        async def send_typing(self, **kwargs: object) -> dict[str, object]:
            return {}

    async def scenario() -> None:
        client = SlowClient()
        with FakeILink() as fake:
            task = asyncio.create_task(
                serve_wechat(
                    config=_config(fake, workspace, state_dir, max_polls=1),
                    client=client,  # type: ignore[arg-type]
                    state=WechatState(state_dir),
                    task_executor=_completed(workspace),
                    stop_event=stop,
                    allow_insecure_local_http=True,
                )
            )
            await asyncio.wait_for(client.get_called.wait(), timeout=2)
            stop.set()
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
