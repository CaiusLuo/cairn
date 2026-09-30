"""Tool-owned argument validation must precede permission checks and execution."""

import asyncio
import json
from pathlib import Path
from typing import NoReturn
from unittest.mock import AsyncMock

import pytest

from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, ToolCall, ToolResult
from cairn.core.permissions import (
    PermissionCapability,
    PermissionDecision,
    PermissionResult,
    PermissionSource,
)
from cairn.observability.models import SpanStatus
from cairn.observability.tracer import Tracer
from cairn.tools.base import InvalidArguments
from cairn.tools.bash import BashTool
from cairn.tools.files import EditFileTool, ReadFileTool
from cairn.workspace.workspace import Workspace
from tests.loop_support import (
    TEST_BUDGET,
    NetworkRequestTool,
    RecordingSink,
    SequenceLLM,
    make_agent,
)
from tests.sandbox_support import require_working_sandbox


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"command": None},
        {"command": 123, "network_access": True, "justification": "required"},
        {"command": "", "justification": "optional"},
        {"command": "   "},
        {"command": "pwd\0"},
        {"command": "pwd", "unexpected": "x", "justification": "valid"},
        {
            "command": "curl example.com",
            "network_access": True,
            "justification": "valid",
            "unexpected": "x",
        },
        {"command": "pwd", "network_access": "true"},
        {"command": "pwd", "network_access": True},
        {
            "command": "curl example.com",
            "network_access": True,
            "justification": "  ",
        },
        {"command": "pwd", "justification": 123},
    ],
)
def test_malformed_bash_fails_before_permission_or_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    events: list[Event] = []
    sink = RecordingSink()
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="call-invalid", name="bash", arguments=arguments)
                ]
            ),
            LLMResponse(content="recovered"),
        ]
    )
    tool = BashTool(Workspace(tmp_path))
    agent = make_agent(llm, tool, events)
    agent.tracer = Tracer(sink)

    def unexpected_permission(tool_call: ToolCall) -> NoReturn:
        pytest.fail("invalid arguments reached the permission handler")

    agent.permission_handler = unexpected_permission

    async def unexpected_subprocess(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("invalid arguments started a subprocess")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_subprocess)

    assert asyncio.run(run_turn(agent, "try malformed call", budget=TEST_BUDGET)) == (
        "recovered"
    )

    failure = json.loads(agent.state.messages[2].content or "")
    assert failure["type"] == "InvalidArguments"
    assert failure["error"]
    assert json.loads(llm.calls[1][0][-1].content or "") == failure
    assert [event.type for event in events if event.type == "tool_error"] == [
        "tool_error"
    ]
    assert (
        next(event for event in events if event.type == "tool_error").data["error_type"]
        == "InvalidArguments"
    )
    assert not [span for span in sink.spans if span.name == "permission.check"]
    tool_spans = [span for span in sink.spans if span.name == "tool.execute"]
    assert len(tool_spans) == 1
    assert tool_spans[0].status == SpanStatus.ERROR
    assert tool_spans[0].attributes["error_type"] == "InvalidArguments"


def test_direct_bash_execute_defensively_validates_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = BashTool(Workspace(tmp_path))
    launched = False

    async def unexpected_subprocess(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal launched
        launched = True
        pytest.fail("direct execute launched an invalid command")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_subprocess)

    with pytest.raises(InvalidArguments):
        asyncio.run(tool.execute({"command": 123}))

    assert not launched


@pytest.mark.parametrize(
    "tool_kind, arguments",
    [
        ("read", {"path": "existing.txt", "start_line": 0}),
        ("read", {"path": "existing.txt", "start_line": 1, "end_line": 201}),
        ("read", {"path": "existing.txt", "unexpected": True}),
        ("edit", {"path": "new.txt", "old_text": "", "new_text": 123}),
        (
            "edit",
            {
                "path": "new.txt",
                "old_text": "",
                "new_text": "content",
                "unexpected": True,
            },
        ),
    ],
)
def test_file_tool_validation_and_direct_execute_have_no_write_effects(
    tmp_path: Path,
    tool_kind: str,
    arguments: dict[str, object],
) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "existing.txt").write_text("line\n", encoding="utf-8")
    tool = ReadFileTool(workspace) if tool_kind == "read" else EditFileTool(workspace)

    with pytest.raises(InvalidArguments):
        tool.validate(arguments)
    with pytest.raises(InvalidArguments):
        asyncio.run(tool.execute(arguments))

    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "line\n"
    assert not (tmp_path / "new.txt").exists()


def test_valid_edit_validation_does_not_create_nested_path(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    tool = EditFileTool(workspace)
    arguments = {"path": "nested/new.txt", "old_text": "", "new_text": "content"}

    tool.validate(arguments)

    assert not (tmp_path / "nested").exists()


def test_shell_syntax_error_is_reported_by_bash_inside_native_sandbox(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    require_working_sandbox(workspace)
    events: list[Event] = []
    sink = RecordingSink()
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call-syntax",
                        name="bash",
                        arguments={"command": "printf '"},
                    )
                ]
            ),
            LLMResponse(content="recovered"),
        ]
    )
    agent = make_agent(llm, BashTool(workspace), events)
    agent.tracer = Tracer(sink)

    assert asyncio.run(
        run_turn(agent, "try invalid shell syntax", budget=TEST_BUDGET)
    ) == ("recovered")

    tool_result = json.loads(agent.state.messages[2].content or "")
    assert tool_result["exit_code"] != 0
    assert tool_result["stderr"]
    assert "InvalidArguments" not in tool_result["stderr"]
    permission_span = next(
        span for span in sink.spans if span.name == "permission.check"
    )
    assert permission_span.attributes["policy_decision"] == "allow"
    assert not [event for event in events if event.type == "tool_error"]
    tool_span = next(span for span in sink.spans if span.name == "tool.execute")
    assert tool_span.status == SpanStatus.ERROR


def test_sudo_policy_denial_skips_permissive_handler_and_execution() -> None:
    events: list[Event] = []
    sink = RecordingSink()
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call-sudo", name="bash", arguments={"command": "sudo true"}
                    )
                ]
            ),
            LLMResponse(content="recovered"),
        ]
    )
    tool = NetworkRequestTool()
    agent = make_agent(llm, tool, events)
    agent.tracer = Tracer(sink)

    def allow_everything(tool_call: ToolCall) -> NoReturn:
        pytest.fail("policy denial reached the permissive handler")

    agent.permission_handler = allow_everything

    assert asyncio.run(run_turn(agent, "try sudo", budget=TEST_BUDGET)) == "recovered"
    assert tool.calls == []
    failure = json.loads(agent.state.messages[2].content or "")
    assert failure == {"error": "sudo is not supported", "type": "PolicyDenied"}
    assert [
        (event.type, event.data["error"])
        for event in events
        if event.type == "tool_denied"
    ] == [("tool_denied", "sudo is not supported")]
    permission_span = next(
        span for span in sink.spans if span.name == "permission.check"
    )
    assert permission_span.attributes["source"] == "policy_deny"
    assert permission_span.attributes["policy_decision"] == "deny"
    assert not [span for span in sink.spans if span.name == "tool.execute"]


@pytest.mark.parametrize(
    "source", [PermissionSource.USER_ONCE, PermissionSource.USER_DENIED]
)
@pytest.mark.parametrize("granted", [False, True])
def test_provenance_does_not_determine_network_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: PermissionSource,
    granted: bool,
) -> None:
    call = ToolCall(
        id="network",
        name="bash",
        arguments={
            "command": "true",
            "network_access": True,
            "justification": "test",
        },
    )
    tool = BashTool(Workspace(tmp_path))
    execute = AsyncMock(return_value=ToolResult(exit_code=0))
    monkeypatch.setattr(tool, "execute", execute)
    agent = make_agent(
        SequenceLLM([LLMResponse(tool_calls=[call]), LLMResponse(content="done")]),
        tool,
    )

    def approve(tool_call: ToolCall) -> PermissionResult:
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=True,
            source=source,
            granted_capabilities=(
                frozenset({PermissionCapability.NETWORK}) if granted else frozenset()
            ),
        )

    agent.permission_handler = approve
    asyncio.run(run_turn(agent, "test", budget=TEST_BUDGET))
    execute.assert_awaited_once()
    assert execute.call_args.kwargs["context"].network_access is granted
