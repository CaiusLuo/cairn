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
from cairn.terminal.wechat.serve import ServeConfig, serve_wechat
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
                    state_dir=tmp_path / "state",
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
        await asyncio.sleep(30)
        return HeadlessResult(
            task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=str(workspace)
        )

    with FakeILink() as fake:
        fake.updates = [
            {"ret": 0, "msgs": [_msg("slow task", mid=1)], "get_updates_buf": "c1"},
            {"ret": 0, "msgs": [_msg("/status", mid=2)], "get_updates_buf": "c2"},
            {"ret": 0, "msgs": [_msg("another", mid=3)], "get_updates_buf": "c3"},
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
        assert calls == ["slow task"]
        assert "正在执行" in replies
        assert "当前有任务正在执行" in replies
        assert "取消" in replies
        record = WechatState(state_dir).get_record("msg:1")
        assert record is not None and record["state"] == "cancelled"


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
    reply_text = str(fake.sent[0].get("item_list"))
    assert "completed" in reply_text
    assert "task finished" in reply_text


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
