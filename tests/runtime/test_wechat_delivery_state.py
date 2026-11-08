"""Durable WeChat delivery intent facts in protected host state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.infra.wechat.state import WechatState


def _begin(
    state: WechatState,
    *,
    message_key: str = "msg:stable",
    message_id: str = "message-1",
    account_id: str = "bot-1",
    sender_id: str = "owner",
    content: str = "hello",
    request_id: str = "request-1",
) -> str:
    return state.begin_delivery_intent(
        message_key=message_key,
        message_id=message_id,
        account_id=account_id,
        sender_id=sender_id,
        context_token="private-context",
        content=content,
        request_id=request_id,
    )


def test_intent_starts_awaiting_admission_and_binds_run(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    key = _begin(state)
    row = state.get_delivery_intent(key)
    assert row is not None
    assert row["intent_key"] == key
    assert row["request_id"] == "request-1"
    assert row["admission_status"] == "awaiting_admission"
    assert row["delivery_status"] == "pending"

    bound = state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
    assert bound["conversation_id"] == "conv-1"
    assert bound["run_id"] == "run-1"
    assert bound["admission_status"] == "accepted"

    reopened = WechatState(tmp_path / "wechat")
    persisted = reopened.get_delivery_intent(key)
    assert persisted is not None
    assert persisted["run_id"] == "run-1"
    assert [item["intent_key"] for item in reopened.list_delivery_intents()] == [key]


def test_same_message_identity_requires_same_account_sender_and_content(
    tmp_path: Path,
) -> None:
    state = WechatState(tmp_path / "wechat")
    key = _begin(state)
    assert _begin(state) == key
    with pytest.raises(ValueError, match=r"different content|identity"):
        _begin(state, content="changed")
    with pytest.raises(ValueError, match=r"different content|identity"):
        _begin(state, account_id="bot-2")
    with pytest.raises(ValueError, match=r"different content|identity"):
        _begin(state, sender_id="other")


def test_sent_is_terminal_and_only_one_sender_wins(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    key = _begin(state)
    state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
    assert state.begin_delivery_attempt(key) is True
    assert state.begin_delivery_attempt(key) is False
    sent = state.mark_delivery_sent(key)
    assert sent["delivery_status"] == "sent"
    with pytest.raises(ValueError, match="sent"):
        state.mark_delivery_unconfirmed(key)


def test_unconfirmed_cannot_be_started_again(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    key = _begin(state)
    state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
    assert state.begin_delivery_attempt(key) is True
    state.mark_delivery_unconfirmed(key)
    assert state.begin_delivery_attempt(key) is False
    current = state.get_delivery_intent(key)
    assert current is not None
    assert current["delivery_status"] == "unconfirmed"


def test_immutable_conversation_and_run_bindings(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    key = _begin(state)
    state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
    with pytest.raises(ValueError, match="immutable"):
        state.bind_delivery_intent(key, conversation_id="conv-2")
    with pytest.raises(ValueError, match="immutable"):
        state.bind_delivery_intent(key, run_id="run-2")


def test_damaged_delivery_record_is_not_ignored(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    _begin(state)
    raw = json.loads(state.state_path.read_text())
    raw["delivery_intents"]["broken"] = {"status": "???"}
    state.state_path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="delivery"):
        WechatState(tmp_path / "wechat").load()
