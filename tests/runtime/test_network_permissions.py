"""Local shell approval: a command runs only after the session is trusted.

Cairn no longer sandboxes shell commands, so it makes no network-isolation
claim. What still holds is the approval contract: a ``bash`` call is an ASK, a
denial runs nothing, and an approval lets the command actually reach the host.
"""

import asyncio
import json
import shlex
import socket
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionRequest,
    PermissionSource,
    SessionPermissionHandler,
)
from cairn.infra.observability.models import SpanStatus
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.bash import BashTool
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.loop import run_turn
from tests.support.runtime import (
    TEST_BUDGET,
    RecordingSink,
    SequenceLLM,
    shell_tool_response,
)
from tests.support.sandbox import require_local_shell, sandbox_python


class AlternatePermissionChoice(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"


def host_connection_command(port: int) -> str:
    python = shlex.quote(sandbox_python())
    script = (
        "import socket; print('started', flush=True); "
        f"s=socket.create_connection(('127.0.0.1', {port}), timeout=2); "
        "s.close(); print('connected')"
    )
    return f"{python} -c {shlex.quote(script)}"


@pytest.fixture
def host_network_command() -> Iterator[str]:
    require_local_shell()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        yield host_connection_command(listener.getsockname()[1])


def shell_call(index: int, command: str) -> ToolCall:
    return ToolCall(id=str(index), name="bash", arguments={"command": command})


@pytest.mark.parametrize("choice", list(PermissionChoice))
def test_shell_runs_only_after_approval(
    tmp_path: Path, choice: PermissionChoice, host_network_command: str
) -> None:
    prompts: list[PermissionRequest] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return choice

    handler = SessionPermissionHandler(prompt)
    calls = [
        shell_call(0, host_network_command),
        shell_call(1, host_network_command),
    ]
    sink = RecordingSink()
    agent = build_agent(
        workspace=Workspace(tmp_path),
        event_handler=None,
        llm=SequenceLLM([LLMResponse(tool_calls=calls), LLMResponse(content="done")]),
        permission_handler=handler,
        tracer=Tracer(sink),
    )
    assert asyncio.run(run_turn(agent, "test", budget=TEST_BUDGET)) == "done"

    messages = [
        json.loads(m.content or "") for m in agent.state.messages if m.role == "tool"
    ]
    spans = [s for s in sink.spans if s.name == "permission.check"]

    if choice is PermissionChoice.DENY:
        for message in messages:
            assert message["error"] == "Permission denied by user."
            assert message["type"] == "PermissionDenied"
            assert message["blocked_paths"] == []
        assert len([s for s in sink.spans if s.name == "tool.execute"]) == 0
        return

    # An approved shell really reaches the host network: no isolation is claimed.
    for message in messages:
        assert message["exit_code"] == 0
        assert message["stdout"] == "started\nconnected\n"

    assert spans[0].attributes["granted_capabilities"] == ["local_shell"]
    reused = choice is PermissionChoice.ALLOW_SESSION
    assert len(prompts) == (1 if reused else 2)
    assert spans[1].attributes["source"] == (
        PermissionSource.SESSION_GRANT.value
        if reused
        else PermissionSource.USER_ONCE.value
    )


def test_session_trust_prompts_once_for_every_later_command(tmp_path: Path) -> None:
    prompts: list[PermissionRequest] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return PermissionChoice.ALLOW_SESSION

    handler = SessionPermissionHandler(prompt)
    first = handler(shell_call(0, "true"))
    second = handler(shell_call(1, "false"))

    assert first.allowed and first.prompted
    assert second.allowed and not second.prompted
    assert second.source is PermissionSource.SESSION_GRANT
    assert first.granted_capabilities == frozenset({PermissionCapability.LOCAL_SHELL})
    assert len(prompts) == 1


@pytest.mark.parametrize(
    "choice",
    [
        None,
        "3",
        "allow_once",
        "allow_session",
        AlternatePermissionChoice.ALLOW_ONCE,
        AlternatePermissionChoice.ALLOW_SESSION,
    ],
)
def test_run_turn_rejects_invalid_prompt_choices_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, choice: object
) -> None:
    prompt = Mock(return_value=choice)
    handler = SessionPermissionHandler(prompt)
    sink = RecordingSink()
    execute = AsyncMock()
    monkeypatch.setattr(BashTool, "execute", execute)
    agent = build_agent(
        workspace=Workspace(tmp_path),
        event_handler=None,
        llm=SequenceLLM([shell_tool_response()]),
        permission_handler=handler,
        tracer=Tracer(sink),
    )

    with pytest.raises(ValueError, match="Invalid permission choice"):
        asyncio.run(run_turn(agent, "test", budget=TEST_BUDGET))

    assert prompt.call_count == 1
    assert handler.grants == set()
    assert execute.await_count == 0
    assert not any(span.name == "tool.execute" for span in sink.spans)
    permission_span = next(
        span for span in sink.spans if span.name == "permission.check"
    )
    turn_span = next(span for span in sink.spans if span.name == "agent.turn")
    assert permission_span.status is SpanStatus.ERROR
    assert turn_span.status is SpanStatus.ERROR
    assert turn_span.error is not None
    assert "Invalid permission choice" in turn_span.error


def test_no_handler_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without anyone to approve, a shell command is not executed."""
    execute = AsyncMock()
    monkeypatch.setattr(BashTool, "execute", execute)
    agent = build_agent(
        workspace=Workspace(tmp_path),
        event_handler=None,
        llm=SequenceLLM([shell_tool_response(), LLMResponse(content="done")]),
        permission_handler=None,
        tracer=None,
    )

    assert asyncio.run(run_turn(agent, "test", budget=TEST_BUDGET)) == "done"
    assert execute.await_count == 0
