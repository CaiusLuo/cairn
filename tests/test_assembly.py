import asyncio
import builtins
import json
from pathlib import Path

import pytest

from cairn.assembly import build_agent
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import PermissionDecision, PermissionResult
from cairn.workspace.workspace import Workspace
from tests.loop_support import SequenceLLM


def test_build_agent_runs_headless_tool_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_input(_prompt: str = "") -> str:
        pytest.fail("Headless execution requested terminal input")

    monkeypatch.setattr(builtins, "input", unexpected_input)
    workspace = Workspace(tmp_path)
    tool_call = ToolCall(
        id="create-answer",
        name="edit_file",
        arguments={"path": "answer.txt", "old_text": "", "new_text": "42\n"},
    )
    llm = SequenceLLM(
        [LLMResponse(tool_calls=[tool_call]), LLMResponse(content="done")]
    )
    permission_calls: list[ToolCall] = []

    def allow_handler(tool_call: ToolCall) -> PermissionResult:
        permission_calls.append(tool_call)
        return PermissionResult(
            policy_decision=PermissionDecision.ALLOW,
            allowed=True,
            prompted=False,
        )

    agent = build_agent(
        workspace=workspace,
        llm=llm,
        permission_handler=allow_handler,
        event_handler=None,
        tracer=None,
    )
    result = asyncio.run(run_turn(agent, "Create answer.txt"))

    assert result == "done"
    assert (tmp_path / "answer.txt").is_file()
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "42\n"
    assert agent.permission_handler is allow_handler
    assert permission_calls == [tool_call]
    assert agent.event_handler is None
    assert agent.tracer is None
    assert len(llm.calls) == 2
    tool_message = llm.calls[1][0][-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == tool_call.id
    assert json.loads(tool_message.content or "") == {
        "stdout": "Created answer.txt",
        "stderr": "",
        "exit_code": 0,
    }
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
