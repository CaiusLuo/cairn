import hashlib
import json
from uuid import uuid4

from cairn.agent.turn.events import Event, SessionEvent
from cairn.development.execution.timeline import (
    ARGUMENT_PREVIEW_CHARS,
    ERROR_PREVIEW_CHARS,
    project_event,
)
from cairn.shared.redaction import redact_value

SECRET = "{{SECRET_ab12cd34}}"


def test_each_kind_publishes_only_its_contract_fields() -> None:
    event = Event(
        type="model_response",
        data={
            "step": 2,
            "attempt": 1,
            "model_id": "group/one",
            "duration_ms": 12.5,
            "input_tokens": 3,
            "output_tokens": 4,
            "tool_call_count": 0,
            "has_content": False,
            "prompt": "conversation content",
            "error": "unrelated exception",
        },
    )
    assert project_event(event) == {
        "step": 2,
        "attempt": 1,
        "model_id": "group/one",
        "duration_ms": 12.5,
        "input_tokens": 3,
        "output_tokens": 4,
        "tool_call_count": 0,
        "has_content": False,
    }


def test_future_event_kind_publishes_nothing_until_its_fields_are_named() -> None:
    event = Event.model_construct(
        type="future_event", data={"trace_id": "abc", "conversation": "content"}
    )
    assert project_event(event) == {}


def test_session_envelope_correlates_ids_and_tool_metadata_without_output() -> None:
    session_id, turn_id = uuid4(), uuid4()
    event = SessionEvent(
        type="tool_result",
        data={
            "tool": "bash",
            "tool_call_id": "call-1",
            "exit_code": 1,
            "duration_ms": 8,
            "stdout": "full output",
            "stderr": "full error",
            "stdout_length": 11,
            "stderr_length": 10,
            "stdout_truncated": False,
            "stderr_truncated": True,
        },
        session_id=session_id,
        turn_id=turn_id,
        sequence=7,
    )
    assert project_event(event) == {
        "tool": "bash",
        "tool_call_id": "call-1",
        "exit_code": 1,
        "duration_ms": 8,
        "stdout_length": 11,
        "stderr_length": 10,
        "stdout_truncated": False,
        "stderr_truncated": True,
        "session_id": str(session_id),
        "turn_id": str(turn_id),
    }


def test_tool_error_text_is_redacted_and_bounded() -> None:
    error = SECRET + "x" * (ERROR_PREVIEW_CHARS + 50)
    data = project_event(
        Event(
            type="tool_error",
            data={
                "tool": "bash",
                "tool_call_id": "call-1",
                "error": error,
                "error_type": "BashError",
                "failure_kind": "permanent",
            },
        ),
        sensitive_values=(SECRET,),
    )
    assert SECRET not in data["error"]
    assert data["error_truncated"] is True
    assert len(data["error"]) == ERROR_PREVIEW_CHARS


def test_tool_arguments_are_a_redacted_preview_plus_digest() -> None:
    arguments = {"api_key": SECRET, "nested": {"token": SECRET}, "value": 42}
    data = project_event(
        Event(
            type="tool_call",
            data={
                "tool": "bash",
                "tool_call_id": "call-1",
                "arguments": arguments,
            },
        ),
        sensitive_values=(SECRET,),
    )
    assert SECRET not in data["arguments_preview"]
    encoded = json.dumps(
        redact_value(arguments, (SECRET,)), ensure_ascii=False, sort_keys=True
    )
    assert data["arguments_digest"] == hashlib.sha256(encoded.encode()).hexdigest()
    assert data["arguments_truncated"] is False

    oversized = project_event(
        Event(
            type="tool_call",
            data={"tool": "bash", "arguments": {"value": "x" * 5000}},
        )
    )
    assert oversized["arguments_truncated"] is True
    assert len(oversized["arguments_preview"]) == ARGUMENT_PREVIEW_CHARS


def test_agent_finish_publishes_length_not_content() -> None:
    data = project_event(
        Event(type="agent_finish", data={"step": 3, "content": "secret answer"})
    )
    assert data == {"step": 3, "content_length": 13}


def test_trace_persistence_failure_is_a_stable_visible_fact() -> None:
    data = project_event(
        Event(
            type="trace_finish",
            data={
                "trace_id": "a" * 32,
                "status": "ok",
                "persisted": False,
                "persistence_error": "OSError: " + SECRET + " disk unavailable",
                "error_code": "sink_failed",
                "turn_ms": 15.5,
            },
        ),
        sensitive_values=(SECRET,),
    )
    assert data["persisted"] is False
    assert data["error_code"] == "sink_failed"
    assert data["turn_ms"] == 15.5
    assert SECRET not in data["persistence_error"]
    assert data["persistence_error_truncated"] is False
