"""Explicit built-in tool assembly; execution and approval stay with runtime."""

from cairn.infra.tools.bash import BashTool
from cairn.infra.tools.files import EditFileTool, ReadFileTool
from cairn.infra.tools.glob import GlobTool
from cairn.infra.tools.grep import GrepTool
from cairn.infra.tools.patch import PatchTool
from cairn.infra.tools.registry import ToolRegistry
from cairn.infra.tools.write import WriteTool
from cairn.infra.workspace.workspace import Workspace


def build_default_registry(
    workspace: Workspace, *, secret_env_keys: frozenset[str] = frozenset()
) -> ToolRegistry:
    """Create a fresh registry in model-visible declaration order.

    All tools share the exact Workspace; Bash receives the session credential
    policy. Callers can instrument registered tools with ``override_tool``.
    Construction grants no capabilities and performs no permission decisions.
    """
    registry = ToolRegistry()
    registry.register_tool(BashTool(workspace, secret_env_keys=secret_env_keys))
    registry.register_tool(ReadFileTool(workspace))
    registry.register_tool(EditFileTool(workspace))
    registry.register_tool(WriteTool(workspace))
    registry.register_tool(PatchTool(workspace))
    registry.register_tool(GlobTool(workspace))
    registry.register_tool(GrepTool(workspace))
    return registry
