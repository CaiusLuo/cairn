import asyncio
import json
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from cairn.assembly import build_agent
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import (
    PermissionChoice,
    PermissionRequest,
    SessionPermissionHandler,
)
from cairn.observability.tracer import Tracer
from cairn.tools.base import ToolExecutionContext
from cairn.tools.bash import BashTool
from cairn.workspace.workspace import Workspace
from tests.loop_support import TEST_BUDGET, RecordingSink, SequenceLLM


def socket_command() -> str:
    python = shlex.quote(
        sys.executable if sys.platform == "darwin" else "/usr/bin/python3"
    )
    script = (
        "import socket; print('started', flush=True); "
        "s=socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); print('bound')"
    )
    return f"{python} -c {shlex.quote(script)}"


@pytest.mark.parametrize("requested", [None, False, True])
def test_arguments_cannot_grant_network(tmp_path: Path, requested: bool | None) -> None:
    arguments: dict[str, object] = {"command": socket_command()}
    if requested is not None:
        arguments.update(network_access=requested, justification="local socket")
    result = asyncio.run(BashTool(Workspace(tmp_path)).execute(arguments))
    assert result.exit_code != 0
    assert result.stdout == "started\n"


@pytest.mark.parametrize("choice", list(PermissionChoice))
def test_network_end_to_end(tmp_path: Path, choice: PermissionChoice) -> None:
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
                "command": socket_command(),
                "network_access": requested,
                "justification": "local socket",
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
            assert messages[index]["stdout"] == "started\nbound\n"
        assert spans[index].attributes["policy_decision"] == "ask"
        assert spans[index].attributes["allowed"] is (not denied)
        assert spans[index].attributes["prompted"] is (
            index == 0 or choice != PermissionChoice.ALLOW_SESSION
        )
        assert (
            spans[index].attributes["source"]
            == {
                PermissionChoice.DENY: "user_denied",
                PermissionChoice.ALLOW_ONCE: "user_once",
                PermissionChoice.ALLOW_SESSION: "session_grant",
            }[choice]
        )
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


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_network_context_changes_only_network_restriction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
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


def test_no_handler_does_not_execute_network_request(tmp_path: Path) -> None:
    sink = RecordingSink()
    agent = build_agent(
        workspace=Workspace(tmp_path),
        event_handler=None,
        llm=SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="1",
                            name="bash",
                            arguments={
                                "command": "touch should-not-exist",
                                "network_access": True,
                                "justification": "test",
                            },
                        )
                    ]
                ),
                LLMResponse(content="done"),
            ]
        ),
        tracer=Tracer(sink),
        permission_handler=None,
    )
    asyncio.run(run_turn(agent, "test", budget=TEST_BUDGET))
    assert not (tmp_path / "should-not-exist").exists()
    span = next(s for s in sink.spans if s.name == "permission.check")
    assert span.attributes["source"] == "no_handler"
    assert span.attributes["granted_capabilities"] == []
    assert span.attributes["allowed"] is False
