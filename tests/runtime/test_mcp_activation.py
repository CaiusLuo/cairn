"""MCP activation: authorize before start, real stdio discovery/call, fail-closed tools."""

import asyncio
import sys
from pathlib import Path

import pytest

from cairn.core.budget import RunBudget
from cairn.core.events import Event
from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import PermissionCapability, SessionPermissionHandler
from cairn.infra.mcp.config import McpServerConfig
from cairn.infra.mcp.stdio import McpStdioClient, McpTimeout, McpToolSpec
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.loop import run_turn
from cairn.runtime.mcp.activation import McpToolAdapter, open_mcp_servers
from cairn.terminal.headless import HeadlessStatus, run_headless_task
from tests.support.runtime import SequenceLLM

SERVER = Path(__file__).resolve().parents[1] / "support" / "mcp_server.py"


def _cfg() -> McpServerConfig:
    return McpServerConfig(
        name="demo",
        transport="stdio",
        command=sys.executable,
        args=("-u", str(SERVER)),
        env_keys=("MCP_TEST_MARKER",),
    )


def _handler(tmp_path: Path, *, grant: bool) -> SessionPermissionHandler:
    handler = SessionPermissionHandler(
        prompt=None, workspace_root=tmp_path, home=tmp_path
    )
    if grant:
        handler.grants.update(
            {
                PermissionCapability.MCP_SERVER,
                PermissionCapability.MCP_TOOL,
                PermissionCapability.LOCAL_SHELL,
            }
        )
    return handler


def test_unauthorized_activation_never_starts_the_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "started.marker"
    monkeypatch.setenv("MCP_TEST_MARKER", str(marker))
    handler = _handler(tmp_path, grant=False)

    async def scenario() -> None:
        with pytest.raises(PermissionError):
            async with open_mcp_servers(
                configs=(_cfg(),), handler=handler, workspace=tmp_path
            ):
                pass

    asyncio.run(scenario())
    assert not marker.exists()  # no process was spawned


def test_authorized_activation_lists_and_calls_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "started.marker"
    monkeypatch.setenv("MCP_TEST_MARKER", str(marker))
    handler = _handler(tmp_path, grant=True)

    async def scenario() -> None:
        async with open_mcp_servers(
            configs=(_cfg(),), handler=handler, workspace=tmp_path
        ) as runtime:
            names = [tool.name for tool in runtime.tools]
            assert names == ["mcp__demo__echo"]
            result = await runtime.tools[0].execute({"text": "hello"})
            assert result.exit_code == 0
            assert result.stdout == "hello"

    asyncio.run(scenario())
    assert marker.exists()


def test_http_mcp_reports_blocker_without_connecting(tmp_path: Path) -> None:
    handler = _handler(tmp_path, grant=False)
    handler.grants.update(
        {
            PermissionCapability.MCP_SERVER,
            PermissionCapability.MCP_TOOL,
            PermissionCapability.NETWORK,
        }
    )
    config = McpServerConfig(
        name="remote", transport="streamable_http", url="http://127.0.0.1:9/mcp"
    )

    async def scenario() -> None:
        with pytest.raises(NotImplementedError):
            async with open_mcp_servers(
                configs=(config,), handler=handler, workspace=tmp_path
            ):
                pass

    asyncio.run(scenario())


def test_dynamic_tool_is_fail_closed_without_grant(tmp_path: Path) -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> dict[str, object]:
            self.calls += 1
            return {
                "content": [{"type": "text", "text": "should not run"}],
                "isError": False,
            }

    client = _FakeClient()
    adapter = McpToolAdapter(
        client=client,  # type: ignore[arg-type]
        server="demo",
        spec=McpToolSpec("echo", "Echo", {"type": "object", "properties": {}}),
    )
    events: list[Event] = []
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name=adapter.name, arguments={"text": "x"})
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    handler = SessionPermissionHandler(
        prompt=None, workspace_root=tmp_path, home=tmp_path
    )

    def _capture(event: Event) -> None:
        events.append(event)

    agent = build_agent(
        workspace=Workspace(tmp_path),
        llm=llm,
        permission_handler=handler,
        event_handler=_capture,
        tracer=None,
        summarize=False,
    )
    agent.tools.register_tool(adapter)

    asyncio.run(run_turn(agent, "call it", budget=RunBudget(max_steps=3)))

    denied = [e for e in events if e.type == "tool_denied"]
    assert denied and denied[0].data["tool"] == adapter.name
    assert client.calls == 0  # execution never reached the server


def test_headless_unauthorized_mcp_reports_permission_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "started.marker"
    monkeypatch.setenv("MCP_TEST_MARKER", str(marker))
    llm = SequenceLLM([LLMResponse(content="never")])
    result = asyncio.run(
        run_headless_task(
            workspace=tmp_path,
            prompt="x",
            grant_workspace_trust=True,
            grant_shell=True,
            llm=llm,
            budget=RunBudget(max_steps=3),
            mcp_configs=(_cfg(),),
            grant_mcp=False,
        )
    )
    assert result.status is HeadlessStatus.UNAUTHORIZED
    assert result.error_category == "mcp_permission_denied"
    assert llm.calls == []
    assert not marker.exists()


def test_headless_authorized_mcp_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "started.marker"
    monkeypatch.setenv("MCP_TEST_MARKER", str(marker))
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="mcp__demo__echo", arguments={"text": "hi"})
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    result = asyncio.run(
        run_headless_task(
            workspace=tmp_path,
            prompt="x",
            grant_workspace_trust=True,
            grant_shell=True,
            llm=llm,
            budget=RunBudget(max_steps=5),
            mcp_configs=(_cfg(),),
            grant_mcp=True,
        )
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert marker.exists()
    second_messages = llm.calls[1][0]
    assert any("hi" in str(message.content) for message in second_messages)


def test_terminal_activation_registers_and_removes_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.terminal import cli as cli_module

    marker = tmp_path / "started.marker"
    monkeypatch.setenv("MCP_TEST_MARKER", str(marker))
    handler = _handler(tmp_path, grant=True)
    agent = build_agent(
        workspace=Workspace(tmp_path),
        llm=SequenceLLM([]),
        permission_handler=handler,
        event_handler=None,
        tracer=None,
        summarize=False,
    )

    async def scenario() -> None:
        async with cli_module._selected_mcp_tools(agent, (_cfg(),), ()):
            assert "mcp__demo__echo" in agent.tools.tools
        assert "mcp__demo__echo" not in agent.tools.tools

    asyncio.run(scenario())
    assert marker.exists()


def test_mcp_invalid_arguments_are_preflight_failures(tmp_path: Path) -> None:
    from cairn.infra.tools.base import InvalidArguments

    adapter = McpToolAdapter(
        client=object(),  # type: ignore[arg-type]
        server="demo",
        spec=McpToolSpec(
            "echo",
            "Echo",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
    )
    with pytest.raises(InvalidArguments):
        adapter.validate({})
    with pytest.raises(InvalidArguments):
        adapter.validate({"text": 3})


def test_stdio_start_failure_closes_the_child(tmp_path: Path) -> None:
    client = McpStdioClient(
        command=sys.executable,
        args=("-c", "import time; time.sleep(30)"),
        env_keys=(),
        cwd=tmp_path,
        timeout=0.2,
    )
    with pytest.raises(McpTimeout):
        asyncio.run(client.start())
    assert client._process is None
