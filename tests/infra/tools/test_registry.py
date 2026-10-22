import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from cairn.core.models import ToolResult
from cairn.infra.tools import build_default_registry
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext, ToolNotFound
from cairn.infra.tools.bash import BashTool
from cairn.infra.tools.registry import ToolRegistry
from cairn.infra.workspace.workspace import Workspace


class EchoTool:
    name = "echo"
    description = "Return the supplied value."

    def __init__(self) -> None:
        self.context: ToolExecutionContext | None = None

    def schema(self) -> dict[str, Any]:
        return {"name": self.name}

    def validate(self, arguments: dict[str, Any]) -> None:
        if "value" not in arguments:
            raise InvalidArguments("echo requires a value")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.context = context
        return ToolResult(stdout=str(arguments["value"]), exit_code=0)


class MissingTool(EchoTool):
    name = "missing"


def test_registry_rejects_duplicate_and_missing_tools() -> None:
    registry = ToolRegistry()
    tool = EchoTool()

    registry.register_tool(tool)
    with pytest.raises(ValueError, match="Tool already registered: echo"):
        registry.register_tool(tool)
    with pytest.raises(ToolNotFound, match="Tool not found: missing"):
        registry.get_tool("missing")


def test_registry_exposes_schemas_and_executes_tools() -> None:
    registry = ToolRegistry()
    tool = EchoTool()
    registry.register_tool(tool)

    context = ToolExecutionContext()
    result = asyncio.run(
        registry.execute(
            "echo",
            {"value": "hello"},
            context=context,
        )
    )

    assert registry.get_tool("echo") is tool
    assert registry.schemas() == [{"name": "echo"}]
    assert result == ToolResult(stdout="hello", exit_code=0)
    assert tool.context is context


def test_registry_tools_are_read_only() -> None:
    registry = ToolRegistry()
    tool = EchoTool()
    registry.register_tool(tool)

    assert registry.tools["echo"] is tool
    assert set(registry.tools) == {"echo"}
    with pytest.raises(TypeError):
        cast(Any, registry.tools)["other"] = tool


def test_registry_override_replaces_only_registered_tools() -> None:
    registry = ToolRegistry()
    registry.register_tool(EchoTool())
    wrapper = EchoTool()

    registry.override_tool(wrapper)

    assert registry.get_tool("echo") is wrapper
    assert set(registry.tools) == {"echo"}
    with pytest.raises(ToolNotFound, match="Tool not found: missing"):
        registry.override_tool(MissingTool())


def test_default_registry_keeps_order_schemas_and_session_injection(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    secret_keys = frozenset({"CUSTOM_PROVIDER_TOKEN"})
    registry = build_default_registry(workspace, secret_env_keys=secret_keys)
    expected = (
        "bash",
        "read_file",
        "edit_file",
        "write_file",
        "patch_file",
        "glob",
        "grep",
    )
    assert tuple(registry.tools) == expected
    assert (
        tuple(schema["function"]["name"] for schema in registry.schemas()) == expected
    )
    for tool in registry.tools.values():
        assert cast(Any, tool).workspace is workspace
    bash = registry.get_tool("bash")
    assert isinstance(bash, BashTool)
    assert bash.secret_env_keys == secret_keys

    # Each session owns its instances; instrumentation must not leak to another.
    other = build_default_registry(workspace)
    replacement = BashTool(workspace)
    registry.override_tool(replacement)
    assert registry.get_tool("bash") is replacement
    assert other.get_tool("bash") is not replacement
    assert tuple(registry.tools) == expected
