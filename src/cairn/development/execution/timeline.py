"""Explicit field contracts for the shared Runtime's execution timeline.

Run events are read by every client, so an Agent event does not become public
just because it was emitted. Each kind names the diagnostic fields it may
publish here; a new field stays private until it is added to that kind's
contract. This is deliberately not one allow-list: a token count, a tool exit
code and a phase duration have different owners and different guarantees.

Only metadata, counts, fixed class names and bounded summaries cross this
boundary. Tool arguments are reduced to a redacted preview plus digest, and
error text is redacted and truncated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from cairn.agent.turn.events import Event, SessionEvent
from cairn.shared.redaction import redact_text, redact_value

ARGUMENT_PREVIEW_CHARS = 4096
ERROR_PREVIEW_CHARS = 1024
PERSISTENCE_PREVIEW_CHARS = 512

FIELDS: Mapping[str, tuple[str, ...]] = {
    "turn_started": (),
    "turn_settled": ("outcome", "trace_finalization_failed"),
    "turn_committed": (
        "checkpoint_seq",
        "revision",
        "outcome",
        "encode_ms",
        "persist_ms",
        "checkpoint_bytes",
        "checkpoint_messages",
    ),
    "model_stream_start": ("step", "attempt", "model_id"),
    "model_first_token": (
        "step",
        "attempt",
        "model_id",
        "first_token_ms",
        "output",
    ),
    "model_stream_end": (
        "step",
        "attempt",
        "model_id",
        "status",
        "context_used",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
    ),
    "model_request": (
        "step",
        "attempt",
        "model_id",
        "model_name",
        "context_prepare_ms",
        "context_tokens_before",
        "context_tokens_after",
        "context_max_tokens",
        "context_estimate",
        "context_compacted",
        "context_compacted_messages",
        "context_compaction_tokens_saved",
        "context_folded_tool_results",
        "context_omitted_turns",
        "context_omitted_messages",
        "fallback_reason",
        "fallback_attempt",
    ),
    "model_response": (
        "step",
        "attempt",
        "model_id",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "tool_call_count",
        "has_content",
    ),
    "model_error": (
        "step",
        "attempt",
        "model_id",
        "duration_ms",
        "error_type",
        "cancelled",
    ),
    "agent_step": ("step", "max_steps", "context_limit", "budget"),
    "permission_check": (
        "tool",
        "tool_call_id",
        "allowed",
        "policy_decision",
        "prompted",
        "source",
        "duration_ms",
    ),
    "tool_call": ("tool", "tool_call_id"),
    "tool_result": (
        "tool",
        "tool_call_id",
        "exit_code",
        "duration_ms",
        "stdout_length",
        "stderr_length",
        "stdout_truncated",
        "stderr_truncated",
    ),
    "tool_error": (
        "tool",
        "tool_call_id",
        "error_type",
        "failure_kind",
        "duration_ms",
    ),
    "tool_denied": (
        "tool",
        "tool_call_id",
        "error_type",
        "failure_kind",
        "duration_ms",
    ),
    "agent_finish": ("step",),
    "agent_budget_exhausted": ("reason", "limit", "used"),
    "context_trimmed": ("omitted_turns", "omitted_messages"),
    "context_compacted": (
        "step",
        "status",
        "folded_messages",
        "tokens_before",
        "tokens_after",
        "duration_ms",
        "input_tokens",
        "output_tokens",
    ),
    "trace_start": ("trace_id",),
    "trace_finish": (
        "trace_id",
        "status",
        "persisted",
        "persistence_error",
        "error_code",
        "turn_ms",
        "usage",
    ),
}

_ERROR_KINDS = frozenset({"tool_error", "tool_denied"})


def project_event(
    event: Event, *, sensitive_values: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Return the public diagnostic fields for one Runtime event."""
    if event.type == "tool_call":
        data = _bounded_text_fields(event.data, FIELDS.get("tool_call", ()))
        data.update(_argument_summary(event.data.get("arguments"), sensitive_values))
    else:
        data = _bounded_text_fields(event.data, FIELDS.get(event.type, ()))

    if event.type in _ERROR_KINDS:
        error = event.data.get("error")
        if isinstance(error, str):
            redacted = redact_text(error, sensitive_values)
            data["error"] = redacted[:ERROR_PREVIEW_CHARS]
            data["error_truncated"] = len(redacted) > ERROR_PREVIEW_CHARS

    if event.type == "trace_finish":
        error = event.data.get("persistence_error")
        if isinstance(error, str):
            redacted = redact_text(error, sensitive_values)
            data["persistence_error"] = redacted[:PERSISTENCE_PREVIEW_CHARS]
            data["persistence_error_truncated"] = (
                len(redacted) > PERSISTENCE_PREVIEW_CHARS
            )

    if event.type == "agent_finish":
        content = event.data.get("content")
        if isinstance(content, str):
            # The transcript owns the answer; the timeline needs only its size.
            data["content_length"] = len(content)

    if isinstance(event, SessionEvent):
        data.setdefault("session_id", str(event.session_id))
        data.setdefault("turn_id", str(event.turn_id))
    return data


def _bounded_text_fields(
    source: Mapping[str, Any], fields: tuple[str, ...]
) -> dict[str, Any]:
    # ``False`` is a fact (denied, not truncated), so only ``None`` is omitted.
    return {
        name: source[name]
        for name in fields
        if name in source and source[name] is not None
    }


def _argument_summary(
    arguments: Any, sensitive_values: tuple[str, ...]
) -> dict[str, Any]:
    redacted = redact_value(
        arguments if arguments is not None else {}, sensitive_values
    )
    encoded = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
    return {
        "arguments_preview": encoded[:ARGUMENT_PREVIEW_CHARS],
        "arguments_truncated": len(encoded) > ARGUMENT_PREVIEW_CHARS,
        "arguments_digest": hashlib.sha256(encoded.encode()).hexdigest(),
    }
