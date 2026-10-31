"""Offline tests for the long-running serve scheduler entry point."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cairn.core.schedule import ScheduleJob, ScheduleKind
from cairn.infra.schedule.store import ScheduleStore
from cairn.infra.wechat.ilink import ILinkClient
from cairn.infra.wechat.state import WechatAccount, WechatServeLock, WechatState
from cairn.terminal.headless import HeadlessResult, HeadlessStatus
from cairn.terminal.wechat.serve import ServeConfig, serve_wechat
from tests.support.fake_ilink import FakeILink

NOW = datetime.now(ZoneInfo("Asia/Shanghai"))


def _config(fake: FakeILink, workspace: Path, state_dir: Path) -> ServeConfig:
    return ServeConfig(
        workspace=workspace,
        state_dir=state_dir,
        account=WechatAccount(
            bot_token="token-serve-schedule",
            ilink_bot_id="bot-serve",
            base_url=fake.url,
            ilink_user_id="owner",
        ),
        approval=None,
        allowed_senders=frozenset({"owner"}),
        schedule_store=state_dir / "schedule",
        schedule_poll_interval=0.05,
        max_polls=100,
        poll_interval=0.005,
    )


def _client(fake: FakeILink) -> ILinkClient:
    return ILinkClient(
        base_url=fake.url, bot_token="token-serve-schedule", allow_insecure_local=True
    )


def test_serve_entry_runs_due_task_and_pushes_to_authorized_owner(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "wechat"
    state = WechatState(state_dir)
    state.load()["messages"]["msg:1"] = {
        "sender": "owner",
        "context_token": "ctx-serve",
        "updated": 1.0,
    }
    state.flush()
    schedule = ScheduleStore(state_dir / "schedule")
    schedule.save_jobs(
        (
            ScheduleJob(
                job_id="job-serve",
                name="serve schedule",
                prompt="scheduled prompt",
                workspace=str(workspace),
                recipient="owner",
                account_id="bot-serve",
                kind=ScheduleKind.ONCE,
                at=NOW,
                created_at=NOW,
                next_run_at=NOW,
            ),
        )
    )
    calls: list[str] = []

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        calls.append(prompt)
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            final_response="scheduled result token-serve-schedule",
            workspace=str(workspace),
        )

    with FakeILink() as fake:
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir),
                client=_client(fake),
                state=state,
                task_executor=executor,
                allow_insecure_local_http=True,
            )
        )
        assert calls == ["scheduled prompt"]
        assert fake.sent
        sent_text = str(fake.sent[0].get("item_list"))
        assert "scheduled result" in sent_text
        assert "token-serve-schedule" not in sent_text
        assert fake.sent[0]["context_token"] == "ctx-serve"
        run = schedule.load_runs()[0]
        assert "token-serve-schedule" not in str(run.get("result"))


def test_scheduler_does_not_start_when_account_state_mismatches(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "wechat"
    state = WechatState(state_dir)
    state.save_account(
        WechatAccount(
            bot_token="token-a",
            ilink_bot_id="bot-A",
            base_url="https://example.test",
            ilink_user_id="owner",
        )
    )
    schedule = ScheduleStore(state_dir / "schedule")
    schedule.save_jobs(
        (
            ScheduleJob(
                job_id="job-mismatch",
                prompt="must not run",
                workspace=str(workspace),
                recipient="owner",
                account_id="bot-B",
                kind=ScheduleKind.ONCE,
                at=NOW,
                created_at=NOW,
                next_run_at=NOW,
            ),
        )
    )
    called = False

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        nonlocal called
        called = True
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    with FakeILink() as fake:
        with pytest.raises(ValueError, match="different bot account"):
            asyncio.run(
                serve_wechat(
                    config=_config(fake, workspace, state_dir),
                    client=_client(fake),
                    state=state,
                    task_executor=executor,
                    allow_insecure_local_http=True,
                )
            )
        import time

        time.sleep(0.25)
        assert called is False


def test_scheduler_does_not_start_when_serve_lock_is_held(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "wechat"
    state = WechatState(state_dir)
    schedule = ScheduleStore(state_dir / "schedule")
    schedule.save_jobs(
        (
            ScheduleJob(
                job_id="job-lock",
                prompt="must not run",
                workspace=str(workspace),
                recipient="owner",
                account_id="bot-serve",
                kind=ScheduleKind.ONCE,
                at=NOW,
                created_at=NOW,
                next_run_at=NOW,
            ),
        )
    )
    called = False

    async def executor(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        nonlocal called
        called = True
        return HeadlessResult(
            task_id=task_id,
            status=HeadlessStatus.COMPLETED,
            workspace=str(workspace),
        )

    lock = WechatServeLock(state_dir)
    lock.acquire(description="test holder")
    try:
        with FakeILink() as fake, pytest.raises(Exception, match="already owns"):
            asyncio.run(
                serve_wechat(
                    config=_config(fake, workspace, state_dir),
                    client=_client(fake),
                    state=state,
                    task_executor=executor,
                    allow_insecure_local_http=True,
                )
            )
        import time

        time.sleep(0.25)
        assert called is False
    finally:
        lock.release()


def test_known_credential_values_ignore_ordinary_env_settings(tmp_path: Path) -> None:
    from cairn.terminal.wechat.serve import _known_credential_values

    workspace = tmp_path / "ws"
    (workspace / ".cairn").mkdir(parents=True)
    (workspace / ".env").write_text(
        "DEBUG=1\nPROVIDER_KEY=fake-provider-key\n", encoding="utf-8"
    )
    (workspace / ".cairn" / "models.toml").write_text(
        'base_url = "https://x.test/v1"\napi_key_env = "PROVIDER_KEY"\n'
        '\n[[models]]\nname = "m"\nmodel_ids = ["m"]\n',
        encoding="utf-8",
    )
    values = _known_credential_values(workspace, ("wechat-token",))
    assert "fake-provider-key" in values
    assert "wechat-token" in values
    assert "1" not in values


def test_wechat_serve_command_passes_default_schedule_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import cairn.terminal.wechat.app as app_module
    from cairn.infra.wechat.state import WechatAccount, WechatState

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "wechat"
    WechatState(state_dir).save_account(
        WechatAccount(
            bot_token="token-cli",
            ilink_bot_id="bot-cli",
            base_url="https://example.test",
            ilink_user_id="owner",
        )
    )
    schedule_dir = tmp_path / "schedule"
    monkeypatch.setenv("CAIRN_SCHEDULE_DIR", str(schedule_dir))

    async def fake_serve(**kwargs: object) -> object:
        fake_serve.captured = kwargs  # type: ignore[attr-defined]
        from cairn.terminal.wechat.serve import ServeReport

        return ServeReport()

    monkeypatch.setattr(app_module, "serve_wechat", fake_serve)
    result = CliRunner().invoke(
        app_module.wechat_app,
        [
            "serve",
            "--workspace",
            str(workspace),
            "--allow-sender",
            "owner",
            "--trust-workspace",
            "--state-dir",
            str(state_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    config = fake_serve.captured["config"]  # type: ignore[attr-defined]
    assert config.schedule_store == schedule_dir


def test_default_executor_does_not_read_credentials_before_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn.terminal.wechat.serve as serve_module
    from cairn.infra.wechat.state import WechatAccount, WechatState
    from cairn.terminal.wechat.serve import ServeConfig, default_task_executor

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "wechat"
    state = WechatState(state_dir)
    state.save_account(
        WechatAccount(
            bot_token="fake-token",
            ilink_bot_id="bot-test",
            base_url="https://example.test",
            ilink_user_id="owner",
        )
    )
    config = ServeConfig(
        workspace=workspace,
        state_dir=state_dir,
        account=WechatAccount(
            bot_token="fake-token",
            ilink_bot_id="bot-test",
            base_url="https://example.test",
            ilink_user_id="owner",
        ),
        approval=None,
        allowed_senders=frozenset({"owner"}),
    )
    reads: list[str] = []

    def spy_dotenv(*_args: object, **_kwargs: object) -> dict[str, str]:
        reads.append("env")
        return {}

    monkeypatch.setattr(serve_module, "dotenv_values", spy_dotenv)
    default_task_executor(config)
    assert reads == []


def test_serve_default_executor_redacts_provider_secret_in_send_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn.terminal.wechat.serve as serve_module

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "wechat"
    state = WechatState(state_dir)
    state.load()["messages"]["msg:1"] = {
        "sender": "owner",
        "context_token": "ctx-provider",
        "updated": 1.0,
    }
    state.flush()
    schedule = ScheduleStore(state_dir / "schedule")
    schedule.save_jobs(
        (
            ScheduleJob(
                job_id="job-provider",
                prompt="scheduled prompt",
                workspace=str(workspace),
                recipient="owner",
                account_id="bot-serve",
                kind=ScheduleKind.ONCE,
                at=NOW,
                created_at=NOW,
                next_run_at=NOW,
            ),
        )
    )

    async def fake_config_run(**kwargs: object) -> HeadlessResult:
        callback = kwargs.get("on_secrets_resolved")
        if callable(callback):
            callback(("fake-provider-key",))
        return HeadlessResult(
            task_id="scheduled-job-provider",
            status=HeadlessStatus.COMPLETED,
            final_response="result fake-provider-key token-serve-schedule",
            workspace=str(workspace),
        )

    monkeypatch.setattr(serve_module, "_run_via_config_async", fake_config_run)
    with FakeILink() as fake:
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir),
                client=_client(fake),
                state=state,
                allow_insecure_local_http=True,
            )
        )
        sent = str(fake.sent[0].get("item_list")) if fake.sent else ""
        assert "fake-provider-key" not in sent
        assert "token-serve-schedule" not in sent
        assert "[REDACTED]" in sent
    run = schedule.load_runs()[0]
    assert "fake-provider-key" not in str(run.get("result"))
    assert "token-serve-schedule" not in str(run.get("result"))


def test_serve_default_executor_redacts_provider_secret_in_error_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn.terminal.wechat.serve as serve_module

    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "wechat"
    state = WechatState(state_dir)
    schedule = ScheduleStore(state_dir / "schedule")
    schedule.save_jobs(
        (
            ScheduleJob(
                job_id="job-provider-error",
                prompt="scheduled prompt",
                workspace=str(workspace),
                recipient="owner",
                account_id="bot-serve",
                kind=ScheduleKind.ONCE,
                at=NOW,
                created_at=NOW,
                next_run_at=NOW,
            ),
        )
    )

    async def fake_config_run(**kwargs: object) -> HeadlessResult:
        callback = kwargs.get("on_secrets_resolved")
        if callable(callback):
            callback(("fake-provider-key",))
        raise RuntimeError("failure fake-provider-key token-serve-schedule")

    monkeypatch.setattr(serve_module, "_run_via_config_async", fake_config_run)
    with FakeILink() as fake:
        asyncio.run(
            serve_wechat(
                config=_config(fake, workspace, state_dir),
                client=_client(fake),
                state=state,
                allow_insecure_local_http=True,
            )
        )
    run = schedule.load_runs()[0]
    assert "fake-provider-key" not in str(run.get("error"))
    assert "token-serve-schedule" not in str(run.get("error"))
