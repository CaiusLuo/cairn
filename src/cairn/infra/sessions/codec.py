"""Strict full-history and incremental encoding, without provider/runtime objects."""

import hashlib
import json
import re
from collections.abc import Sequence

from pydantic import ValidationError

from cairn.core.memory.models import MemoryError, check_sensitive
from cairn.core.models import Message, ToolCall
from cairn.infra.sessions.models import (
    MAX_CHECKPOINT_BYTES,
    MAX_INVOCATIONS,
    MAX_MESSAGE_BYTES,
    MAX_RECORD_BYTES,
    CheckpointDelta,
    CheckpointPayload,
    CheckpointReceipt,
    SessionStoreError,
    StoredMessage,
    StoredToolCall,
    StrictDTO,
)

# Also recognize JSON key/value credentials, including nested argument JSON.
_CREDENTIAL_FIELD = re.compile(
    r'(?i)"(?:api[_-]?key|access[_-]?token|password|secret|authorization)"\s*:\s*"[^"\s]+'
)


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _nonfinite(value: str) -> object:
    raise ValueError("Nonfinite JSON")


def parse(raw: bytes, limit: int) -> object:
    if len(raw) > limit:
        raise SessionStoreError("record_limit")
    try:
        # Strict UTF-8, including rejection of invalid/lone surrogate code points.
        value: object = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite
        )
        if canonical(value) != raw:
            raise ValueError("Noncanonical JSON")
        return value
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise SessionStoreError("invalid_record") from None


def _sensitive(value: object, sensitive_values: tuple[str, ...]) -> None:
    if isinstance(value, str):
        check_sensitive(value, sensitive_values)
        if _CREDENTIAL_FIELD.search(value):
            raise SessionStoreError("sensitive_record")
    elif isinstance(value, dict):
        for key, item in value.items():
            if (
                isinstance(key, str)
                and key.lower()
                in {
                    "api_key",
                    "api-key",
                    "apikey",
                    "access_token",
                    "access-token",
                    "accesstoken",
                    "password",
                    "secret",
                    "authorization",
                }
                and isinstance(item, str)
                and item.strip()
            ):
                raise SessionStoreError("sensitive_record")
            _sensitive(key, sensitive_values)
            _sensitive(item, sensitive_values)
    elif isinstance(value, list):
        for item in value:
            _sensitive(item, sensitive_values)


def _source_fields(value: object) -> None:
    if isinstance(value, StrictDTO):
        if set(value.__dict__) - type(value).model_fields.keys():
            raise SessionStoreError("invalid_record")
        for item in value.__dict__.values():
            _source_fields(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _source_fields(item)


def validate_record(
    value: StrictDTO,
    *,
    limit: int = MAX_RECORD_BYTES,
    sensitive_values: tuple[str, ...] = (),
) -> None:
    """Validate an already-decoded record without re-serializing it.

    `encode_record` serializes a model and then re-parses the bytes to validate
    them. Callers that only need the guarantees - and that already hold a model
    produced by `decode_record` - would otherwise pay a full
    dump/canonicalize/parse round-trip per record. This keeps every check that
    matters here (declared fields, size bound, credential and sensitive-value
    filtering) and drops only the redundant round-trip.
    """
    try:
        _source_fields(value)
        raw = canonical(
            value.model_dump(mode="json", warnings="error", serialize_as_any=True)
        )
        if len(raw) > limit:
            raise SessionStoreError("record_limit")
        _sensitive(value.model_dump(mode="json"), sensitive_values)
        text = raw.decode("utf-8")
        check_sensitive(text, sensitive_values)
        if _CREDENTIAL_FIELD.search(text):
            raise SessionStoreError("sensitive_record")
    except SessionStoreError:
        raise
    except MemoryError:
        raise SessionStoreError("sensitive_record") from None
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise SessionStoreError("invalid_record") from None


def encode_record(
    value: StrictDTO,
    *,
    limit: int = MAX_RECORD_BYTES,
    sensitive_values: tuple[str, ...] = (),
) -> bytes:
    try:
        _source_fields(value)
        raw = canonical(
            value.model_dump(mode="json", warnings="error", serialize_as_any=True)
        )
        decode_record(type(value), raw, limit=limit, sensitive_values=sensitive_values)
        return raw
    except SessionStoreError:
        raise
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise SessionStoreError("invalid_record") from None


def decode_record[T: StrictDTO](
    kind: type[T],
    raw: bytes,
    *,
    limit: int = MAX_RECORD_BYTES,
    sensitive_values: tuple[str, ...] = (),
) -> T:
    try:
        data = parse(raw, limit)
        if (
            kind is CheckpointPayload
            and isinstance(data, dict)
            and type(data.get("format_version")) is int
            and data["format_version"] != 1
        ):
            raise SessionStoreError("format_unsupported")
        _sensitive(data, sensitive_values)
        check_sensitive(raw.decode("utf-8"), sensitive_values)
        if _CREDENTIAL_FIELD.search(raw.decode("utf-8")):
            raise SessionStoreError("sensitive_record")
        # JSON strict mode accepts JSON arrays for immutable tuples, but no coercion
        # of strings to integers/bools or unknown fields. Revalidates model_copy.
        checked = kind.model_validate_json(raw, strict=True)
        if canonical(checked.model_dump(mode="json", serialize_as_any=True)) != raw:
            raise SessionStoreError("invalid_record")
        return checked
    except SessionStoreError:
        raise
    except MemoryError:
        raise SessionStoreError("sensitive_record") from None
    except (ValidationError, ValueError, TypeError, RecursionError, OverflowError):
        raise SessionStoreError("invalid_record") from None


def validate_history(
    checkpoint: CheckpointPayload | CheckpointDelta, secrets: tuple[str, ...]
) -> None:
    start = (
        0 if isinstance(checkpoint, CheckpointDelta) else checkpoint.message_range.start
    )
    pending: list[str] = []
    seen_user = False
    tail_calls = 0
    for index, message in enumerate(checkpoint.messages):
        validate_record(message, limit=MAX_MESSAGE_BYTES, sensitive_values=secrets)
        if pending:
            if message.role != "tool" or message.tool_call_id != pending.pop(0):
                raise SessionStoreError("invalid_tool_group")
            continue
        if message.role == "tool" or (message.role == "assistant" and not seen_user):
            raise SessionStoreError("invalid_tool_group")
        if message.role == "user":
            seen_user = True
        if message.tool_calls:
            ids = [call.id for call in message.tool_calls]
            if len(ids) != len(set(ids)):
                raise SessionStoreError("invalid_tool_group")
            pending = ids
        for call in message.tool_calls:
            arguments = parse(call.arguments_json.encode("utf-8"), MAX_MESSAGE_BYTES)
            if not isinstance(arguments, dict):
                raise SessionStoreError("invalid_arguments")
            try:
                _sensitive(arguments, secrets)
            except MemoryError:
                raise SessionStoreError("sensitive_record") from None
        if index >= start:
            tail_calls += len(message.tool_calls)
    if pending:
        raise SessionStoreError("invalid_tool_group")
    if tail_calls > MAX_INVOCATIONS:
        raise SessionStoreError("invocation_limit")
    tail = checkpoint.messages[start:]
    if tail and (tail[0].role != "user" or any(m.role == "user" for m in tail[1:])):
        raise SessionStoreError("invalid_turn_range")
    if checkpoint.outcome == "completed" and (
        not tail or tail[-1].role != "assistant" or tail[-1].tool_calls
    ):
        raise SessionStoreError("invalid_terminal_outcome")


def encode_checkpoint(
    checkpoint: CheckpointPayload, *, sensitive_values: tuple[str, ...] = ()
) -> tuple[bytes, str]:
    raw = encode_record(
        checkpoint, limit=MAX_CHECKPOINT_BYTES, sensitive_values=sensitive_values
    )
    checked = decode_record(CheckpointPayload, raw, limit=MAX_CHECKPOINT_BYTES)
    validate_history(checked, sensitive_values)
    return raw, hashlib.sha256(raw).hexdigest()


def decode_checkpoint(
    raw: bytes, sha256: str, *, sensitive_values: tuple[str, ...] = ()
) -> CheckpointPayload:
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise SessionStoreError("digest_mismatch")
    checkpoint = decode_record(
        CheckpointPayload,
        raw,
        limit=MAX_CHECKPOINT_BYTES,
        sensitive_values=sensitive_values,
    )
    validate_history(checkpoint, sensitive_values)
    return checkpoint


def encode_storage_checkpoint(
    checkpoint: CheckpointPayload,
    predecessor: CheckpointReceipt | None,
    *,
    format_version: int,
    sensitive_values: tuple[str, ...] = (),
) -> tuple[bytes, str]:
    """Encode a validated full host snapshot in the database's chosen format.

    The Store separately verifies exact prefix equality in the current SQL
    transaction. This function grants no admission or authority.
    """
    raw, digest = encode_checkpoint(checkpoint, sensitive_values=sensitive_values)
    if format_version == 1:
        return raw, digest
    if format_version != 2:
        raise SessionStoreError("format_unsupported")
    checked = decode_record(CheckpointPayload, raw, limit=MAX_CHECKPOINT_BYTES)
    delta = CheckpointDelta(
        **checked.model_dump(exclude={"format_version", "messages"}),
        messages=checked.messages[checked.message_range.start :],
        predecessor=predecessor,
    )
    raw = encode_record(
        delta, limit=MAX_CHECKPOINT_BYTES, sensitive_values=sensitive_values
    )
    return raw, hashlib.sha256(raw).hexdigest()


def decode_storage_checkpoint(
    raw: bytes,
    sha256: str,
    *,
    format_version: int,
    sensitive_values: tuple[str, ...] = (),
) -> CheckpointPayload | CheckpointDelta:
    if format_version == 1:
        return decode_checkpoint(raw, sha256, sensitive_values=sensitive_values)
    if format_version != 2:
        raise SessionStoreError("format_unsupported")
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise SessionStoreError("digest_mismatch")
    delta = decode_record(
        CheckpointDelta,
        raw,
        limit=MAX_CHECKPOINT_BYTES,
        sensitive_values=sensitive_values,
    )
    validate_history(delta, sensitive_values)
    return delta


def snapshot_messages(messages: Sequence[Message]) -> tuple[StoredMessage, ...]:
    """Detach the full AgentState history, before ContextBuilder trims requests."""
    try:
        result: list[StoredMessage] = []
        for message in messages:
            if message.role == "system":
                raise SessionStoreError("invalid_record")
            result.append(
                StoredMessage(
                    role=message.role,
                    content=message.content,
                    tool_call_id=message.tool_call_id,
                    tool_calls=tuple(
                        StoredToolCall(
                            id=call.id,
                            name=call.name,
                            arguments_json=canonical(call.arguments).decode("utf-8"),
                        )
                        for call in message.tool_calls
                    ),
                )
            )
        return tuple(result)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise SessionStoreError("invalid_record") from None


def restore_messages(checkpoint: CheckpointPayload) -> list[Message]:
    """Revalidate first, return new permissive core models only at this boundary."""
    raw, digest = encode_checkpoint(checkpoint)
    checked = decode_checkpoint(raw, digest)
    return [
        Message(
            role=message.role,
            content=message.content,
            tool_call_id=message.tool_call_id,
            tool_calls=[
                ToolCall(id=c.id, name=c.name, arguments=json.loads(c.arguments_json))
                for c in message.tool_calls
            ],
        )
        for message in checked.messages
    ]
