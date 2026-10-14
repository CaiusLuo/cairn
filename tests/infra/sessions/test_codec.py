import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cairn.core.models import Message, ToolCall
from cairn.infra.sessions.codec import (
    canonical,
    decode_checkpoint,
    encode_checkpoint,
    encode_record,
    parse,
    restore_messages,
    validate_record,
)
from cairn.infra.sessions.models import (
    MAX_CHECKPOINT_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES,
    MAX_RECORD_BYTES,
    CheckpointPayload,
    MessageRange,
    Revision,
    SessionStoreError,
    StoredMessage,
    StoredToolCall,
    TraceSummary,
)
from tests.support.sessions import SESSION, T0, binding, intent, messages, settled


def test_empty_unicode_full_history_and_detached_core_models(tmp_path: Path) -> None:
    bind = binding(tmp_path)
    empty = CheckpointPayload(
        session_id=SESSION,
        turn_id=None,
        seq=0,
        binding=bind,
        messages=(),
        message_range=MessageRange(start=0, end=0),
        outcome=None,
        started_at_utc=None,
        settled_at_utc=T0,
    )
    raw, digest = encode_checkpoint(empty)
    assert decode_checkpoint(raw, digest) == empty
    user = "检查 🪨 café\n第二行"
    turn = intent(bind, Revision(session_id=SESSION, version=0, head_seq=0), user=user)
    history = messages(user)
    history[1].tool_calls[0].arguments["nested"] = {
        "values": [None, True, 1, 1.25, "中文"]
    }
    # Full state exceeds a hypothetical request window; it is never trimmed here.
    checkpoint = settled(turn, history=history).checkpoint
    encoded, digest = encode_checkpoint(checkpoint)
    assert user.encode() not in encoded  # Newline is JSON-escaped; Unicode stays UTF-8.
    assert "中文".encode() in encoded
    decoded = decode_checkpoint(encoded, digest)
    assert restore_messages(decoded) == history
    history[1].tool_calls[0].arguments["nested"] = "changed"
    restored = restore_messages(decoded)
    restored[0].content = "changed again"
    assert restore_messages(decoded)[0].content == user
    assert "changed" not in decoded.messages[1].tool_calls[0].arguments_json
    with pytest.raises(ValidationError):
        decoded.seq = 99


def test_group_local_call_id_reuse_and_closed_interruption(tmp_path: Path) -> None:
    turn = intent(
        binding(tmp_path), Revision(session_id=SESSION, version=0, head_seq=0)
    )
    history = messages()
    history.extend(messages()[1:3])  # Same ID, separate assistant group is legal.
    checkpoint = settled(turn, history=history, outcome="cancelled").checkpoint
    raw, digest = encode_checkpoint(checkpoint)
    assert decode_checkpoint(raw, digest).messages[-1].role == "tool"
    assert len(restore_messages(checkpoint)) == 6
    rolled_back = settled(turn, history=[], outcome="runtime_error").checkpoint
    assert encode_checkpoint(rolled_back)


@pytest.mark.parametrize(
    "damage",
    [
        "orphan",
        "dangling",
        "duplicate_call",
        "duplicate_result",
        "out_of_order",
        "system",
        "blank_call",
        "bad_arguments",
        "two_users",
    ],
)
def test_invalid_message_groups(tmp_path: Path, damage: str) -> None:
    history = messages()
    if damage == "orphan":
        history.insert(1, Message(role="tool", tool_call_id="other", content="bad"))
    elif damage == "dangling":
        history.pop(2)
    elif damage == "duplicate_call":
        history[1].tool_calls *= 2
    elif damage == "duplicate_result":
        history.insert(3, history[2])
    elif damage == "out_of_order":
        history[1].tool_calls.append(
            ToolCall(id="call-2", name="read_file", arguments={})
        )
        history.insert(2, Message(role="tool", tool_call_id="call-2", content="bad"))
    elif damage == "system":
        history[0].role = "system"
    elif damage == "blank_call":
        history[1].tool_calls[0].id = ""
    elif damage == "bad_arguments":
        history[1].tool_calls[0].arguments = {"n": float("nan")}
    else:
        history.append(Message(role="user", content="second turn"))
    with pytest.raises(SessionStoreError):
        turn = intent(
            binding(tmp_path), Revision(session_id=SESSION, version=0, head_seq=0)
        )
        encode_checkpoint(settled(turn, history=history).checkpoint)


@pytest.mark.parametrize(
    "damage",
    [
        "extra",
        "version",
        "bool_version",
        "counter_string",
        "bool_counter",
        "range",
        "identity",
        "timestamp",
        "terminal",
        "trace",
        "missing_version",
        "missing_trace",
    ],
)
def test_strict_fields_and_envelope_relationships(tmp_path: Path, damage: str) -> None:
    turn = intent(
        binding(tmp_path), Revision(session_id=SESSION, version=0, head_seq=0)
    )
    data = settled(turn).checkpoint.model_dump(mode="json")
    updates: dict[str, dict[str, object]] = {
        "extra": {"extra": True},
        "version": {"format_version": 2},
        "bool_version": {"format_version": True},
        "counter_string": {"seq": "1"},
        "bool_counter": {"seq": True},
        "range": {"message_range": {"start": 5, "end": 4}},
        "identity": {"turn_id": "00000000-0000-1000-8000-000000000002"},
        "timestamp": {"settled_at_utc": "2020-01-01T00:00:00+00:00"},
        "terminal": {"outcome": "abandoned"},
        "trace": {"trace": {"input_tokens": 0}},
        "missing_version": {},
        "missing_trace": {},
    }
    data.update(updates[damage])
    if damage == "missing_version":
        del data["format_version"]
    elif damage == "missing_trace":
        del data["trace"]
    raw = canonical(data)
    with pytest.raises(SessionStoreError):
        decode_checkpoint(raw, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":"\\ud800"}',
        b' {"x":1}',
        b"\xff",
    ],
)
def test_duplicate_nonfinite_noncanonical_and_invalid_utf8(raw: bytes) -> None:
    with pytest.raises(SessionStoreError):
        parse(raw, MAX_RECORD_BYTES)


def test_digest_and_model_copy_are_not_trusted(tmp_path: Path) -> None:
    turn = intent(
        binding(tmp_path), Revision(session_id=SESSION, version=0, head_seq=0)
    )
    checkpoint = settled(turn).checkpoint
    raw, digest = encode_checkpoint(checkpoint)
    with pytest.raises(SessionStoreError, match="digest"):
        decode_checkpoint(raw, "0" * 64)
    with pytest.raises(SessionStoreError):
        encode_checkpoint(checkpoint.model_copy(update={"seq": "bad"}))
    with pytest.raises(SessionStoreError):
        encode_checkpoint(checkpoint.model_copy(update={"format_version": 99}))
    with pytest.raises(SessionStoreError):
        encode_checkpoint(
            checkpoint.model_copy(
                update={"extra": "silently omitted by Pydantic serialization"}
            )
        )
    modified = checkpoint.messages[0].model_copy(update={"extra": "nested extra"})
    with pytest.raises(SessionStoreError):
        encode_checkpoint(
            checkpoint.model_copy(
                update={"messages": (modified, *checkpoint.messages[1:])}
            )
        )
    assert len(digest) == 64


@pytest.mark.parametrize(
    "content",
    [
        "password=sentinel-pass",
        "sk-abcdefghijklmnop",
        "https://user:pass@example.com",
        "-----BEGIN PRIVATE KEY-----",
        '"password":"sentinel-pass"',
        "opaque-秘密",
    ],
)
def test_sensitive_decoded_strings_and_nested_arguments(
    tmp_path: Path, content: str
) -> None:
    bind = binding(tmp_path)
    turn = intent(bind, Revision(session_id=SESSION, version=0, head_seq=0))
    history = messages()
    history[2].content = content
    with pytest.raises(SessionStoreError) as caught:
        encode_checkpoint(
            settled(turn, history=history).checkpoint, sensitive_values=("opaque-秘密",)
        )
    assert content not in str(caught.value)
    history = messages()
    history[1].tool_calls[0].arguments = {"nested": {"password": "sentinel"}}
    with pytest.raises(SessionStoreError, match="sensitive"):
        encode_checkpoint(settled(turn, history=history).checkpoint)
    history[1].tool_calls[0].arguments = {"nested": {"value": "opaque-秘密"}}
    with pytest.raises(SessionStoreError, match="sensitive"):
        encode_checkpoint(
            settled(turn, history=history).checkpoint, sensitive_values=("opaque-秘密",)
        )


def test_exact_message_and_record_byte_bounds() -> None:
    message = StoredMessage(role="user", content="")
    overhead = len(encode_record(message))
    at_limit = message.model_copy(
        update={"content": "x" * (MAX_MESSAGE_BYTES - overhead)}
    )
    assert len(encode_record(at_limit, limit=MAX_MESSAGE_BYTES)) == MAX_MESSAGE_BYTES
    with pytest.raises(SessionStoreError, match="limit"):
        encode_record(
            at_limit.model_copy(update={"content": (at_limit.content or "") + "x"}),
            limit=MAX_MESSAGE_BYTES,
        )
    at_record = message.model_copy(
        update={"content": "x" * (MAX_RECORD_BYTES - overhead)}
    )
    assert len(encode_record(at_record)) == MAX_RECORD_BYTES
    with pytest.raises(SessionStoreError, match="limit"):
        encode_record(
            at_record.model_copy(update={"content": (at_record.content or "") + "x"})
        )


def test_exact_checkpoint_bytes_message_count_and_invocation_bounds(
    tmp_path: Path,
) -> None:
    turn = intent(
        binding(tmp_path), Revision(session_id=SESSION, version=0, head_seq=0)
    )
    base = [
        Message(role="user", content="hello"),
        *[Message(role="assistant", content="x" * 900_000) for _ in range(9)],
        Message(role="assistant", content=""),
    ]
    checkpoint = settled(turn, history=base).checkpoint
    raw, _ = encode_checkpoint(checkpoint)
    padding = MAX_CHECKPOINT_BYTES - len(raw)
    base[-1].content = (base[-1].content or "") + "x" * padding
    checkpoint = settled(turn, history=base).checkpoint
    assert len(encode_checkpoint(checkpoint)[0]) == MAX_CHECKPOINT_BYTES
    base[-1].content += "x"
    with pytest.raises(SessionStoreError, match="limit"):
        encode_checkpoint(settled(turn, history=base).checkpoint)
    history = [
        Message(role="user", content="hello"),
        *[Message(role="assistant", content="") for _ in range(MAX_MESSAGES - 1)],
    ]
    assert (
        len(encode_checkpoint(settled(turn, history=history).checkpoint)[0])
        < MAX_CHECKPOINT_BYTES
    )
    history.append(Message(role="assistant", content=""))
    with pytest.raises(ValidationError):
        settled(turn, history=history)
    calls = [ToolCall(id=str(i), name="read_file", arguments={}) for i in range(256)]
    history = [
        Message(role="user", content="hello"),
        Message(role="assistant", tool_calls=calls),
        *[Message(role="tool", tool_call_id=c.id, content="") for c in calls],
        Message(role="assistant", content=""),
    ]
    assert encode_checkpoint(settled(turn, history=history).checkpoint)
    history.extend(messages()[1:])
    with pytest.raises(SessionStoreError, match="invocation_limit"):
        encode_checkpoint(settled(turn, history=history).checkpoint)


def test_argument_json_size_and_object_contract() -> None:
    raw = canonical({"text": "x" * (MAX_MESSAGE_BYTES - len(canonical({"text": ""})))})
    assert len(raw) == MAX_MESSAGE_BYTES
    assert isinstance(parse(raw, MAX_MESSAGE_BYTES), dict)
    with pytest.raises(SessionStoreError, match="limit"):
        parse(raw.replace(b'"}', b'x"}'), MAX_MESSAGE_BYTES)
    assert TraceSummary().input_tokens is None
    assert json.loads(raw)["text"]


@pytest.mark.parametrize(
    "message",
    [
        StoredMessage(role="user", content="hello"),
        StoredMessage(role="user", content=""),
        StoredMessage(role="user", content="sk-abcdefghijklmnopqrstuvwxyz123456"),
        StoredMessage(role="user", content="bearer token here"),
        StoredMessage(role="assistant", content=None, tool_calls=()),
    ],
)
@pytest.mark.parametrize("secrets", [(), ("bearer token here",)])
def test_validate_record_agrees_with_encode_record(
    message: StoredMessage, secrets: tuple[str, ...]
) -> None:
    """`validate_record` must keep exactly the contract `encode_record` enforced.

    `validate_history` validates every stored message of every checkpoint, so it
    uses the non-serializing path. If the two ever diverge, a stored record
    could pass history validation while failing to re-encode.
    """

    def outcome(record: StoredMessage) -> str:
        try:
            encode_record(record, limit=MAX_MESSAGE_BYTES, sensitive_values=secrets)
        except SessionStoreError as error:
            return error.code
        return "accepted"

    def checked(record: StoredMessage) -> str:
        try:
            validate_record(record, limit=MAX_MESSAGE_BYTES, sensitive_values=secrets)
        except SessionStoreError as error:
            return error.code
        return "accepted"

    assert outcome(message) == checked(message)


def test_validate_record_rejects_credential_field_values() -> None:
    """Nested credential-shaped fields stay rejected on the non-serializing path."""
    message = StoredMessage(
        role="assistant",
        content=None,
        tool_calls=(
            StoredToolCall(
                id="call-1", name="bash", arguments_json='{"api_key":"live"}'
            ),
        ),
    )
    with pytest.raises(SessionStoreError, match="sensitive"):
        validate_record(message, limit=MAX_MESSAGE_BYTES)


def test_validate_record_enforces_the_exact_message_byte_bound() -> None:
    """The size bound must be enforced on the encoded message, not on a proxy."""
    empty = canonical(StoredMessage(role="user", content="").model_dump(mode="json"))
    overhead = len(empty)
    assert overhead <= MAX_MESSAGE_BYTES

    fits = StoredMessage(role="user", content="x" * (MAX_MESSAGE_BYTES - overhead))
    assert len(canonical(fits.model_dump(mode="json"))) == MAX_MESSAGE_BYTES
    validate_record(fits, limit=MAX_MESSAGE_BYTES)

    over = StoredMessage(role="user", content="x" * (MAX_MESSAGE_BYTES - overhead + 1))
    assert len(canonical(over.model_dump(mode="json"))) == MAX_MESSAGE_BYTES + 1
    with pytest.raises(SessionStoreError, match="limit"):
        validate_record(over, limit=MAX_MESSAGE_BYTES)
