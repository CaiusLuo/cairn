import asyncio
import builtins
import subprocess
from pathlib import Path

import pytest

from cairn.assembly import build_agent
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import PermissionDecision, PermissionResult
from cairn.repo.context import RepositoryContext
from cairn.workspace.workspace import Workspace
from tests.support.runtime import TEST_BUDGET, SequenceLLM


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _initialize_repository(root: Path) -> Path:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Cairn Tests")
    _git(root, "config", "user.email", "cairn-tests@example.invalid")
    tracked = root / "answer.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(root, "add", "--", tracked.name)
    _git(root, "commit", "-m", "initial")
    return tracked


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
    result = asyncio.run(run_turn(agent, "Create answer.txt", budget=TEST_BUDGET))

    assert result == "done"
    assert (tmp_path / "answer.txt").is_file()
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "42\n"
    assert agent.permission_handler is allow_handler
    # edit_file inside the sandbox is a baseline operation: the approval handler
    # is wired but never consulted.
    assert permission_calls == []
    assert agent.event_handler is None
    assert agent.tracer is None
    assert len(llm.calls) == 2
    tool_message = llm.calls[1][0][-1]
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == tool_call.id
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert all(message.role != "system" for message in agent.state.messages)
    assert all(
        "Runtime workspace context:" not in (message.content or "")
        for message in agent.state.messages
    )


def test_build_agent_refreshes_repo_context_without_persisting_it(
    tmp_path: Path,
) -> None:
    tracked = _initialize_repository(tmp_path)
    workspace = Workspace(tmp_path)
    tool_call = ToolCall(
        id="edit-tracked",
        name="edit_file",
        arguments={
            "path": tracked.name,
            "old_text": "before\n",
            "new_text": "after\n",
        },
    )
    llm = SequenceLLM(
        [LLMResponse(tool_calls=[tool_call]), LLMResponse(content="done")]
    )
    agent = build_agent(
        workspace=workspace,
        llm=llm,
        permission_handler=None,
        event_handler=None,
        tracer=None,
    )

    result = asyncio.run(run_turn(agent, "Update answer.txt", budget=TEST_BUDGET))

    assert result == "done"
    assert tracked.read_text(encoding="utf-8") == "after\n"
    clean_prompt = RepositoryContext(
        workspace_root=workspace.root,
        repository_root=workspace.root,
        branch="main",
        dirty=False,
    ).to_prompt()
    dirty_prompt = RepositoryContext(
        workspace_root=workspace.root,
        repository_root=workspace.root,
        branch="main",
        dirty=True,
        changed_files=(tracked.name,),
    ).to_prompt()
    prompts_by_step = [
        [
            message.content
            for message in messages
            if message.role == "system"
            and message.content is not None
            and message.content.startswith("Runtime repository context:")
        ]
        for messages, _tools in llm.calls
    ]
    assert prompts_by_step == [[clean_prompt], [dirty_prompt]]
    assert [message.role for message in agent.state.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert all(message.role != "system" for message in agent.state.messages)
    assert all(
        "Runtime repository context:" not in (message.content or "")
        and "Runtime workspace context:" not in (message.content or "")
        for message in agent.state.messages
    )
