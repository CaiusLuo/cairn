import asyncio
import json
import shlex
import socket
import sys
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from cairn.assembly import build_agent
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import (
    PermissionChoice,
    PermissionRequest,
    PermissionSource,
    SessionPermissionHandler,
)
from cairn.observability.models import SpanStatus
from cairn.observability.tracer import Tracer
from cairn.tools.base import ToolExecutionContext
from cairn.tools.bash import BashTool
from cairn.workspace.workspace import Workspace
from tests.support.runtime import (
    TEST_BUDGET,
    RecordingSink,
    SequenceLLM,
    network_tool_response,
)
from tests.support.sandbox import (
    require_working_sandbox,
    sandbox_python,
    skip_without_sandbox,
)


class AlternatePermissionChoice(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"


def socket_command(port: int) -> str:
    python = shlex.quote(sandbox_python())
    script = (
        "import socket; print('started', flush=True); "
        f"s=socket.create_connection(('127.0.0.1', {port}), timeout=2); "
        "s.close(); print('connected')"
    )
    return f"{python} -c {shlex.quote(script)}"


@pytest.fixture
def host_network_command(tmp_path: Path) -> Iterator[str]:
    require_working_sandbox(Workspace(tmp_path))
    # A Linux network namespace can bind its own loopback sockets. Connecting
    # to a listener outside the sandbox proves access to the host's network.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        # These tests open at most two connections; the listening backlog is
        # sufficient without a background accept thread.
        listener.listen(8)
        yield socket_command(listener.getsockname()[1])


@pytest.mark.parametrize("requested", [None, False, True])
def test_arguments_cannot_grant_network(
    tmp_path: Path, requested: bool | None, host_network_command: str
) -> None:
    arguments: dict[str, object] = {"command": host_network_command}
    if requested is not None:
        arguments.update(network_access=requested, justification="host TCP listener")
    result = asyncio.run(BashTool(Workspace(tmp_path)).execute(arguments))
    assert result.exit_code != 0
    assert result.stdout == "started\n"


@pytest.mark.parametrize("choice", list(PermissionChoice))
def test_network_end_to_end(
    tmp_path: Path, choice: PermissionChoice, host_network_command: str
) -> None:
    prompts: list[PermissionRequest] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return choice

    handler = SessionPermissionHandler(prompt)
    calls = [
        ToolCall(
            id=str(index),
            name="bash",
            arguments={
                "command": host_network_command,
                "network_access": requested,
                "justification": "host TCP listener",
            },
        )
        for index, requested in enumerate((True, True, False))
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
    assert len(prompts) == (1 if choice == PermissionChoice.ALLOW_SESSION else 2)
    for index in (0, 1):
        denied = choice == PermissionChoice.DENY
        if denied:
            assert messages[index] == {
                "error": "Permission denied by user.",
                "type": "PermissionDenied",
            }
        else:
            assert messages[index]["exit_code"] == 0
            assert messages[index]["stdout"] == "started\nconnected\n"
        assert spans[index].attributes["policy_decision"] == "ask"
        assert spans[index].attributes["allowed"] is (not denied)
        assert spans[index].attributes["prompted"] is (
            index == 0 or choice != PermissionChoice.ALLOW_SESSION
        )
        expected_source = {
            PermissionChoice.DENY: PermissionSource.USER_DENIED,
            PermissionChoice.ALLOW_ONCE: PermissionSource.USER_ONCE,
            PermissionChoice.ALLOW_SESSION: (
                PermissionSource.USER_SESSION
                if index == 0
                else PermissionSource.SESSION_GRANT
            ),
        }[choice]
        assert spans[index].attributes["source"] == expected_source
        assert spans[index].attributes["granted_capabilities"] == (
            [] if denied else ["network"]
        )
    assert messages[2]["exit_code"] != 0
    assert messages[2]["stdout"] == "started\n"
    assert spans[2].attributes["source"] == "baseline"
    assert spans[2].attributes["granted_capabilities"] == []
    assert not spans[2].attributes["prompted"]
    if choice == PermissionChoice.DENY:
        assert len([s for s in sink.spans if s.name == "tool.execute"]) == 1


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
    llm = SequenceLLM([network_tool_response()])
    sink = RecordingSink()
    execute = AsyncMock()
    monkeypatch.setattr(BashTool, "execute", execute)
    agent = build_agent(
        workspace=Workspace(tmp_path),
        event_handler=None,
        llm=llm,
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


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_network_context_changes_only_network_restriction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    skip_without_sandbox()
    process = AsyncMock()
    process.pid = 12345
    process.wait.return_value = 0
    for name in ("stdout", "stderr"):
        stream = AsyncMock(spec=asyncio.StreamReader)
        stream.read.return_value = b""
        setattr(process, name, stream)
    create = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr("cairn.tools.bash.os.killpg", Mock())
    monkeypatch.setattr("cairn.tools.bash.sys.platform", platform)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    tool = BashTool(Workspace(tmp_path))
    for allowed in (False, True):
        asyncio.run(
            tool.execute(
                {"command": "true"},
                context=ToolExecutionContext(network_access=allowed),
            )
        )
    denied_args, allowed_args = [list(call.args) for call in create.call_args_list]
    if platform == "darwin":
        assert "(deny network*)" in denied_args[2]
        denied_args[2] = denied_args[2].replace("(deny network*)", "")
    else:
        assert "--unshare-net" in denied_args
        denied_args.remove("--unshare-net")
    assert denied_args == allowed_args
    assert create.call_args_list[0].kwargs == create.call_args_list[1].kwargs


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS host filesystem")
def test_network_grant_preserves_write_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    require_working_sandbox(Workspace(workspace))
    monkeypatch.setenv("TMPDIR", str(workspace))
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    result = asyncio.run(
        BashTool(Workspace(workspace)).execute(
            {"command": f"printf bad > {shlex.quote(str(outside))}"},
            context=ToolExecutionContext(network_access=True),
        )
    )
    assert result.exit_code != 0
    assert outside.read_text() == "unchanged"
