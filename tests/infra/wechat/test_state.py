import json
from pathlib import Path

import pytest

from cairn.integrations.wechat.state import STATE_VERSION, WechatState


def test_queued_message_order_survives_state_reload(tmp_path: Path) -> None:
    root = tmp_path / "state"
    state = WechatState(root)
    for key in ("msg:10", "msg:2", "msg:1"):
        assert state.record_received(key, sender="user", context_token="ctx", text=key)
        state.mark(key, "queued")

    reopened = WechatState(root)
    assert [key for key, _record in reopened.records_in_state("queued")] == [
        "msg:10",
        "msg:2",
        "msg:1",
    ]


@pytest.mark.parametrize(
    ("first", "second", "next_sequence"),
    [(1, 1, 2), (0, 2, 3), (1, 2, 2), ("bad", 2, 3)],
)
def test_invalid_message_order_fails_closed(
    tmp_path: Path, first: object, second: object, next_sequence: int
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "state.json").write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "cursor": "",
                "next_sequence": next_sequence,
                "messages": {
                    "msg:1": {"state": "queued", "sequence": first},
                    "msg:2": {"state": "queued", "sequence": second},
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        WechatState(root).load()


def test_legacy_pending_records_precede_new_messages(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "state.json").write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "cursor": "",
                "messages": {
                    "msg:later": {"state": "queued", "updated": 20},
                    "msg:earlier": {"state": "queued", "updated": 10},
                },
            }
        ),
        encoding="utf-8",
    )
    state = WechatState(root)
    state.record_received("msg:new", sender="user", context_token="ctx", text="new")
    state.mark("msg:new", "queued")
    reopened = WechatState(root)
    assert [key for key, _record in reopened.records_in_state("queued")] == [
        "msg:earlier",
        "msg:later",
        "msg:new",
    ]


def test_record_received_preserves_first_received_at(tmp_path: Path) -> None:
    state = WechatState(tmp_path / "state")
    assert state.record_received("msg:1", sender="user", context_token="ctx", text="hi")
    first = state.get_record("msg:1")
    assert first is not None and isinstance(first.get("received_at"), float)
    stamp = first["received_at"]
    state.mark("msg:1", "chat_replied", updated=stamp + 100)
    record = state.get_record("msg:1")
    assert record is not None
    assert record["received_at"] == stamp
    assert (
        state.record_received("msg:1", sender="user", context_token="ctx", text="retry")
        is False
    )
    retried = state.get_record("msg:1")
    assert retried is not None and retried["received_at"] == stamp
