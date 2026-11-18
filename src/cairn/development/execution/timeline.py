"""Explicit field contracts for the shared Runtime's execution timeline.

Run events are read by every client, so an Agent event does not become public
just because it was emitted. Each kind names the diagnostic fields it may
publish here; a new field stays private until it is added to that kind's
contract. This is deliberately not one allow-list: a token count, a tool exit
code and a phase duration have different owners and different guarantees.

Only metadata, counts, fixed class names and bounded summaries cross this
boundary. Tool arguments are reduced to a redacted preview plus digest, and
free-form error text stays out of the timeline.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from cairn.agent.turn.events import Event, SessionEvent
from cairn.shared.redaction import redact_value

ARGUMENT_PREVIEW_CHARS = 4096
# Optional phase records must never interrupt execution or define durable state.
DIAGNOSTIC_EVENTS = (
    "context_prepare",
    "context_compacted",
    "model_request",
    "model_first_token",
    "model_response",
    "model_error",
    "permission_check",
    "tool_execution_cancelled",
    "trace_start",
    "trace_finish",
)
DIAGNOSTIC_MAX_AGE_DAYS = 30
DIAGNOSTIC_MAX_ROWS = 20_000

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
    "context_prepare": (
        "step",
        "attempt",
        "model_id",
        "phase",
        "duration_ms",
        "status",
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
        "truncated",
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
        "status",
        "error_type",
    ),
    "tool_execution_cancelled": ("tool", "tool_call_id", "duration_ms"),
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
    "agent_finish": ("step", "truncated", "stop_reason", "content_length"),
    "agent_budget_exhausted": ("reason", "limit", "used"),
    "context_trimmed": ("omitted_turns", "omitted_messages"),
    "context_compacted": (
        "compaction_step",
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
        "error_code",
        "turn_ms",
        "usage",
    ),
}


def project_event(
    event: Event, *, sensitive_values: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Return the public diagnostic fields for one Runtime event."""
    if event.type == "tool_call":
        data = _selected_fields(event.data, FIELDS.get("tool_call", ()))
        data.update(_argument_summary(event.data.get("arguments"), sensitive_values))
    else:
        data = _selected_fields(event.data, FIELDS.get(event.type, ()))

    if event.type == "agent_finish":
        content = event.data.get("content")
        if isinstance(content, str):
            # The transcript owns the answer; the timeline needs only its size.
            data["content_length"] = len(content)

    if isinstance(event, SessionEvent):
        data.setdefault("session_id", str(event.session_id))
        data.setdefault("turn_id", str(event.turn_id))
    return {key: redact_value(value, sensitive_values) for key, value in data.items()}


def _selected_fields(
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
