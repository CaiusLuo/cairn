"""Default-on personal memory through the real CLI loop with a fake model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import cairn.terminal.cli as cli_module
from tests.support.cli_harness import configure_cli


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *commands: str) -> Any:
    state = configure_cli(tmp_path, monkeypatch, *commands)
    # The harness disables memory for legacy message-shape tests; this test
    # deliberately exercises the default-on user path.
    monkeypatch.delenv("CAIRN_AGENT_MEMORY", raising=False)
    result = CliRunner().invoke(cli_module.app, [])
    assert result.exit_code == 0, result.output
    return state


def test_note_is_visible_in_the_next_real_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(tmp_path, monkeypatch, "/memory note user prefers tabs", "hello")
    assert state.requests
    messages = state.requests[-1]["messages"]
    text = "\n".join(str(message.get("content", "")) for message in messages)
    assert "user prefers tabs" in text
    assert "personal_agent_memory" in text


def test_local_turn_is_remembered_across_turns_and_provider_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _run(
        tmp_path,
        monkeypatch,
        "first question",
        "/provider use local",
        "second question",
    )
    assert len(state.requests) >= 2
    latest = state.requests[-1]["messages"]
    memory_messages = [
        str(message.get("content", ""))
        for message in latest
        if "personal_agent_memory" in str(message.get("content", ""))
    ]
    assert memory_messages
    assert "first question" in memory_messages[0]


def test_first_cli_request_redacts_saved_wechat_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import cairn.terminal.cli as cli_module
    from cairn.infra.wechat.state import WechatAccount, WechatState

    state_dir = tmp_path.parent / (tmp_path.name + "-wechat-state")
    WechatState(state_dir).save_account(
        WechatAccount(
            bot_token="fake-wechat-token",
            ilink_bot_id="bot-test",
            base_url="https://example.test",
            ilink_user_id="owner",
        )
    )
    monkeypatch.setattr(cli_module, "default_state_dir", lambda: state_dir)
    state = configure_cli(tmp_path, monkeypatch, "hello")
    monkeypatch.delenv("CAIRN_AGENT_MEMORY", raising=False)
    agent_home = Path(os.environ["CAIRN_AGENT_HOME"])
    agent_home.mkdir(parents=True, exist_ok=True)
    (agent_home / "AGENT.md").write_text(
        "# Personal\n\n## Preferences\n- never send fake-wechat-token\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli_module.app, [])
    assert result.exit_code == 0, result.output
    text = "\n".join(
        str(message.get("content", "")) for message in state.requests[0]["messages"]
    )
    assert "fake-wechat-token" not in text
    assert "[REDACTED]" in text
