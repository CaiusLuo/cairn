import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from cairn.core.agent import Agent
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.core.permissions import PermissionDecision, PermissionResult
from cairn.llm.base import LLMClient
from cairn.observability.models import Span
from cairn.observability.tracer import Tracer
from cairn.tools.files import EditFileTool
from cairn.tools.registry import ToolRegistry
from cairn.workspace.workspace import Workspace
from tests.loop_support import FailingLLM, RecordingSink, SequenceLLM


class FailingFollowupLLM(SequenceLLM):
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        if self.calls:
            raise RuntimeError("llm failed")
        return await super().generate(messages, tools)


class WriteThenFailTool(EditFileTool):
    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        await super().execute(arguments)
        raise RuntimeError("tool failed after writing")


def edit_response(*names: str) -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id=f"edit-{index}",
                name="edit_file",
                arguments={"path": name, "old_text": "", "new_text": "42\n"},
            )
            for index, name in enumerate(names, start=1)
        ]
    )


def allow(tool_call: ToolCall) -> PermissionResult:
    return PermissionResult(policy_decision=PermissionDecision.ALLOW, allowed=True)


def make_edit_agent(
    root: Path,
    llm: LLMClient,
    tool_type: type[EditFileTool] = EditFileTool,
) -> Agent:
    registry = ToolRegistry()
    registry.register_tool(tool_type(Workspace(root)))
    return Agent(llm=llm, tools=registry, permission_handler=allow)


@pytest.mark.parametrize("phase", ["llm", "permission", "denied", "event", "trace"])
def test_failure_before_execution_rolls_back_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    llm = FailingFollowupLLM([edit_response("answer.txt")])
    agent = make_edit_agent(tmp_path, FailingLLM() if phase == "llm" else llm)
    agent.state.add_user_message("previous")
    agent.state.add_assistant_message("previous answer")
    previous = agent.state.messages.copy()

    def fail_permission(tool_call: ToolCall) -> PermissionResult:
        raise RuntimeError("permission failed")

    def deny(tool_call: ToolCall) -> PermissionResult:
        return PermissionResult(policy_decision=PermissionDecision.DENY, allowed=False)

    def fail_event(event: Event) -> None:
        if event.type == "tool_call":
            raise RuntimeError("event failed")

    if phase == "permission":
        agent.permission_handler = fail_permission
    elif phase == "denied":
        agent.permission_handler = deny
    elif phase == "event":
        agent.event_handler = fail_event
    elif phase == "trace":
        tracer = Tracer(RecordingSink())
        start_child_span = tracer.start_child_span

        def fail_trace(
            parent: Span, name: str, attributes: dict[str, Any] | None = None
        ) -> Span:
            if name == "tool.execute":
                raise RuntimeError("trace failed")
            return start_child_span(parent, name, attributes)

        monkeypatch.setattr(tracer, "start_child_span", fail_trace)
        agent.tracer = tracer

    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(run_turn(agent, "Create answer.txt"))

    assert agent.state.messages == previous
    assert not (tmp_path / "answer.txt").exists()


def test_edit_fact_survives_llm_failure_and_reaches_next_turn(tmp_path: Path) -> None:
    agent = make_edit_agent(tmp_path, FailingFollowupLLM([edit_response("answer.txt")]))
    agent.state.add_user_message("previous")
    agent.state.add_assistant_message("previous answer")
    previous = agent.state.messages.copy()
    permission_calls: list[ToolCall] = []

    def record_allow(tool_call: ToolCall) -> PermissionResult:
        permission_calls.append(tool_call)
        return allow(tool_call)

    agent.permission_handler = record_allow
    with pytest.raises(RuntimeError, match="llm failed"):
        asyncio.run(run_turn(agent, "Create answer.txt"))

    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "42\n"
    assert agent.state.messages[:2] == previous
    assert [message.role for message in agent.state.messages[2:]] == [
        "user",
        "assistant",
        "tool",
    ]
    fact = agent.state.messages[-1]
    assert fact.tool_call_id == "edit-1"
    assert json.loads(fact.content or "") == {
        "stdout": "Created answer.txt",
        "stderr": "",
        "exit_code": 0,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    preserved = agent.state.messages.copy()
    next_llm = SequenceLLM([LLMResponse(content="done")])
    agent.llm = next_llm

    assert asyncio.run(run_turn(agent, "Continue")) == "done"
    assert next_llm.calls[0][0][1:] == [
        *preserved,
        Message(role="user", content="Continue"),
    ]
    assert len(permission_calls) == 1
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "42\n"
    assert all(message.role != "system" for message in agent.state.messages)
    for message in next_llm.calls[0][0]:
        assert Message.model_validate_json(message.model_dump_json()) == message


def test_unexecuted_calls_are_completed_after_permission_failure(
    tmp_path: Path,
) -> None:
    response = edit_response("first.txt", "second.txt", "third.txt")
    agent = make_edit_agent(tmp_path, SequenceLLM([response]))
    permission_calls: list[str] = []

    def fail_second_permission(tool_call: ToolCall) -> PermissionResult:
        permission_calls.append(tool_call.id)
        if tool_call.id == "edit-2":
            raise RuntimeError("permission failed")
        return allow(tool_call)

    agent.permission_handler = fail_second_permission
    with pytest.raises(RuntimeError, match="permission failed"):
        asyncio.run(run_turn(agent, "Create the files"))

    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "42\n"
    assert not (tmp_path / "second.txt").exists()
    assert not (tmp_path / "third.txt").exists()
    assert permission_calls == ["edit-1", "edit-2"]
    assert [message.role for message in agent.state.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "tool",
    ]
    facts = agent.state.messages[2:]
    assert [fact.tool_call_id for fact in facts] == ["edit-1", "edit-2", "edit-3"]
    assert json.loads(facts[0].content or "")["exit_code"] == 0
    for fact in facts[1:]:
        payload = json.loads(fact.content or "")
        assert payload["type"] == "TurnAborted"
        assert "not executed" in payload["error"].lower()
        assert "permission failed" in payload["error"]


@pytest.mark.parametrize("tool_type", [EditFileTool, WriteThenFailTool])
def test_step_limit_preserves_successful_and_failed_tool_facts(
    tmp_path: Path, tool_type: type[EditFileTool]
) -> None:
    llm = SequenceLLM([edit_response("answer.txt")])
    agent = make_edit_agent(tmp_path, llm, tool_type)

    with pytest.raises(RuntimeError, match="Agent exceeded maximum steps: 1"):
        asyncio.run(run_turn(agent, "Create answer.txt", max_steps=1))

    assert len(llm.calls) == 1
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "42\n"
    assert [message.role for message in agent.state.messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert agent.state.messages[-1].tool_call_id == "edit-1"
    payload = json.loads(agent.state.messages[-1].content or "")
    if tool_type is WriteThenFailTool:
        assert payload == {"type": "RuntimeError", "error": "tool failed after writing"}
    else:
        assert payload == {
            "stdout": "Created answer.txt",
            "stderr": "",
            "exit_code": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }


@pytest.mark.parametrize("tool_type", [EditFileTool, WriteThenFailTool])
def test_tool_fact_is_stored_before_failing_event(
    tmp_path: Path,
    tool_type: type[EditFileTool],
) -> None:
    agent = make_edit_agent(
        tmp_path, SequenceLLM([edit_response("first.txt", "second.txt")]), tool_type
    )
    facts_seen: list[Message] = []

    def fail_event(event: Event) -> None:
        if event.type in {"tool_result", "tool_error"}:
            facts_seen.append(agent.state.messages[-1])
            raise RuntimeError("event failed")

    agent.event_handler = fail_event

    with pytest.raises(RuntimeError, match="event failed"):
        asyncio.run(run_turn(agent, "Create the files"))

    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "42\n"
    assert not (tmp_path / "second.txt").exists()
    assert [message.role for message in agent.state.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    fact, aborted = agent.state.messages[2:]
    assert facts_seen == [fact]
    assert fact.tool_call_id == "edit-1"
    payload = json.loads(fact.content or "")
    if tool_type is WriteThenFailTool:
        assert payload == {"type": "RuntimeError", "error": "tool failed after writing"}
    else:
        assert payload == {
            "stdout": "Created first.txt",
            "stderr": "",
            "exit_code": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
    assert aborted.tool_call_id == "edit-2"
    assert json.loads(aborted.content or "")["type"] == "TurnAborted"


@pytest.mark.parametrize("tool_type", [EditFileTool, WriteThenFailTool])
def test_trace_persistence_failure_does_not_interrupt_tool_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_type: type[EditFileTool],
) -> None:
    agent = make_edit_agent(
        tmp_path,
        SequenceLLM(
            [
                edit_response("first.txt", "second.txt"),
                LLMResponse(content="done"),
            ]
        ),
        tool_type,
    )
    events: list[Event] = []
    agent.event_handler = lambda event: events.append(event)
    facts_seen: list[Message] = []
    sink = RecordingSink()
    emit_span = sink.emit

    def fail_trace(span: Span) -> None:
        if span.name == "tool.execute":
            facts_seen.append(agent.state.messages[-1])
            raise RuntimeError("trace failed")
        emit_span(span)

    monkeypatch.setattr(sink, "emit", fail_trace)
    agent.tracer = Tracer(sink)

    assert asyncio.run(run_turn(agent, "Create the files")) == "done"
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "42\n"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "42\n"
    assert [message.role for message in agent.state.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert facts_seen == [agent.state.messages[2]]
    trace_finish = [event for event in events if event.type == "trace_finish"]
    assert len(trace_finish) == 1
    assert trace_finish[0].data["persisted"] is False
    assert trace_finish[0].data["persistence_error"] == "RuntimeError: trace failed"


@pytest.mark.parametrize("with_tool", [False, True])
def test_final_event_failure_respects_execution_boundary(
    tmp_path: Path,
    with_tool: bool,
) -> None:
    responses = [LLMResponse(content="done")]
    if with_tool:
        responses.insert(0, edit_response("answer.txt"))
    agent = make_edit_agent(tmp_path, SequenceLLM(responses))
    agent.state.add_user_message("previous")
    agent.state.add_assistant_message("previous answer")
    previous = agent.state.messages.copy()

    def fail_event(event: Event) -> None:
        if event.type == "trace_finish":
            raise RuntimeError("event failed")

    agent.tracer = Tracer(RecordingSink())
    agent.event_handler = fail_event

    with pytest.raises(RuntimeError, match="event failed"):
        asyncio.run(run_turn(agent, "hello"))

    assert agent.state.messages[:2] == previous
    if with_tool:
        assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "42\n"
        assert [message.role for message in agent.state.messages[2:]] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        assert json.loads(agent.state.messages[4].content or "")["exit_code"] == 0
    else:
        assert agent.state.messages == previous


@pytest.mark.parametrize("with_tool", [False, True])
def test_final_trace_persistence_failure_keeps_primary_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_tool: bool,
) -> None:
    responses = [LLMResponse(content="done")]
    if with_tool:
        responses.insert(0, edit_response("answer.txt"))
    agent = make_edit_agent(tmp_path, SequenceLLM(responses))
    agent.state.add_user_message("previous")
    agent.state.add_assistant_message("previous answer")
    previous = agent.state.messages.copy()
    events: list[Event] = []
    agent.event_handler = lambda event: events.append(event)
    sink = RecordingSink()
    emit_span = sink.emit

    def fail_trace(span: Span) -> None:
        if span.name == "agent.turn":
            raise RuntimeError("trace failed")
        emit_span(span)

    monkeypatch.setattr(sink, "emit", fail_trace)
    agent.tracer = Tracer(sink)

    assert asyncio.run(run_turn(agent, "hello")) == "done"
    assert agent.state.messages[:2] == previous
    expected_roles = ["user", "assistant"]
    if with_tool:
        expected_roles = ["user", "assistant", "tool", "assistant"]
        assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "42\n"
    assert [message.role for message in agent.state.messages[2:]] == expected_roles
    trace_finish = [event for event in events if event.type == "trace_finish"]
    assert len(trace_finish) == 1
    assert trace_finish[0].data["persisted"] is False
    assert trace_finish[0].data["persistence_error"] == "RuntimeError: trace failed"
