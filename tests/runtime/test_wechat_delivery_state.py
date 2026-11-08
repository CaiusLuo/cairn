"""Durable WeChat delivery intent facts in protected host state."""

from __future__ import annotations

from pathlib import Path

import pytest

from cairn.infra.wechat.state import WechatState


def _intent(state: WechatState) -> str:
    return state.begin_delivery_intent(
        message_key="msg:stable",
        message_id="message-1",
        account_id="bot-1",
        sender_id="owner",
        context_token="private-context",
        content="hello",
    )


def test_delivery_intent_survives_restart_and_binds_run(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    key = _intent(state)
    row = state.get_delivery_intent(key)
    assert row is not None
    assert row["status"] == "pending"
    assert row["run_id"] is None
    assert row["context_token"] == "private-context"

    updated = state.update_delivery_intent(
        key, conversation_id="conv-1", run_id="run-1"
    )
    assert updated["conversation_id"] == "conv-1"
    assert updated["run_id"] == "run-1"

    reopened = WechatState(tmp_path / "wechat")
    persisted = reopened.get_delivery_intent(key)
    assert persisted is not None
    assert persisted["conversation_id"] == "conv-1"
    assert persisted["run_id"] == "run-1"
    assert persisted["status"] == "pending"
    assert [item["run_id"] for item in reopened.list_delivery_intents()] == ["run-1"]


def test_same_message_key_requires_same_content(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    key = _intent(state)
    assert _intent(state) == key
    with pytest.raises(ValueError, match="different content"):
        state.begin_delivery_intent(
            message_key="msg:stable",
            message_id="message-1",
            account_id="bot-1",
            sender_id="owner",
            context_token="private-context",
            content="changed",
        )


def test_missing_message_identity_is_rejected(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    with pytest.raises(ValueError, match="stable message"):
        state.begin_delivery_intent(
            message_key="msg:missing",
            message_id="",
            account_id="bot-1",
            sender_id="owner",
            context_token="private-context",
            content="hello",
        )
