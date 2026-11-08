"""Durable WeChat delivery intent facts in protected host state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


def test_reused_request_id_for_a_different_message_is_rejected(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "wechat")
    _begin(state)
    with pytest.raises(ValueError, match=r"different content|identity"):
        _begin(
            state,
            message_key="msg:other",
            message_id="message-2",
            request_id="request-1",
        )


def test_legacy_state_migrates_sent_and_quarantines_unknown(tmp_path: Path) -> None:
    root = tmp_path / "wechat"
    root.mkdir()
    original = {
        "version": 1,
        "cursor": "",
        "next_sequence": 2,
        "messages": {},
        "delivery_intents": {
            "legacy-sent": {
                "message_digest": "a" * 64,
                "account_digest": "b" * 64,
                "sender_digest": "c" * 64,
                "content_digest": "d" * 64,
                "context_token": "legacy-context",
                "conversation_id": "conv-legacy",
                "run_id": "run-legacy",
                "status": "sent",
                "created_at": 1.0,
                "updated_at": 2.0,
            },
            "legacy-pending": {
                "message_digest": "e" * 64,
                "account_digest": "f" * 64,
                "sender_digest": "1" * 64,
                "content_digest": "2" * 64,
                "context_token": "legacy-context",
                "conversation_id": None,
                "run_id": None,
                "status": "pending",
                "created_at": 3.0,
                "updated_at": 4.0,
            },
        },
    }
    (root / "state.json").write_text(json.dumps(original))

    state = WechatState(root)
    state.load()
    backup = root / "state.pre-b2b2.bak"
    assert backup.exists()
    assert json.loads(backup.read_text()) == original

    rows = {row["legacy_status"]: row for row in state.list_delivery_intents()}
    sent = rows["sent"]
    assert sent["delivery_status"] == "sent"
    assert sent["admission_status"] == "accepted"
    assert sent["needs_manual_review"] is False
    assert sent["run_id"] == "run-legacy"

    pending = rows["pending"]
    assert pending["delivery_status"] == "unconfirmed"
    assert pending["admission_status"] == "admission_unknown"
    assert pending["needs_manual_review"] is True
    assert pending["request_id"] == ""
    assert pending["run_id"] is None
    assert pending["legacy_evidence"]["status"] == "pending"
    assert state.begin_delivery_attempt(pending["intent_key"]) is False


def test_write_failure_does_not_leave_fake_committed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn.infra.wechat.state as state_module

    state = WechatState(tmp_path / "wechat")
    state.begin_delivery_intent(
        message_key="msg:stable",
        message_id="message-1",
        request_id="request-1",
        account_id="bot-1",
        sender_id="owner",
        context_token="context",
        content="hello",
    )
    key = state.list_delivery_intents()[0]["intent_key"]
    state.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
    assert state.begin_delivery_attempt(key) is True
    before = state.get_delivery_intent(key)
    assert before is not None

    def fail_write(_path: Path, _payload: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(state_module, "_atomic_write", fail_write)
    with pytest.raises(OSError, match="disk full"):
        state.mark_delivery_sent(key)
    assert state.get_delivery_intent(key) == before


def test_two_state_instances_share_one_delivery_winner(tmp_path: Path) -> None:
    first = WechatState(tmp_path / "wechat")
    second = WechatState(tmp_path / "wechat")
    key = _begin(first)
    state = second.bind_delivery_intent(key, conversation_id="conv-1", run_id="run-1")
    assert state["admission_status"] == "accepted"
    assert first.begin_delivery_attempt(key) is True
    assert second.begin_delivery_attempt(key) is False
