"""Explicit built-in tool assembly; execution and approval stay with runtime."""

from collections.abc import Sequence

from cairn.infra.skills import SkillLibrary
from cairn.infra.tools.base import Tool
from cairn.infra.tools.bash import BashTool
from cairn.infra.tools.files import EditFileTool, ReadFileTool
from cairn.infra.tools.glob import GlobTool
from cairn.infra.tools.grep import GrepTool
from cairn.infra.tools.patch import PatchTool
from cairn.infra.tools.registry import ToolRegistry
from cairn.infra.tools.skill import ReadSkillResourceTool, ReadSkillTool
from cairn.infra.tools.write import WriteTool
from cairn.infra.workspace.workspace import Workspace


def build_default_registry(
    workspace: Workspace,
    *,
    secret_env_keys: frozenset[str] = frozenset(),
    skills: SkillLibrary | None = None,
    extra_tools: Sequence[Tool] = (),
) -> ToolRegistry:
    """Create a fresh registry in model-visible declaration order.

    All tools share the exact Workspace; Bash receives the session credential
    policy. A supplied ``skills`` library adds the on-demand reader, and its
    advertisements come from the caller that injects the system message. Callers
    can instrument registered tools with ``override_tool``. Construction grants
    no capabilities and performs no permission decisions.
    """
    registry = ToolRegistry()
    registry.register_tool(BashTool(workspace, secret_env_keys=secret_env_keys))
    registry.register_tool(ReadFileTool(workspace))
    registry.register_tool(EditFileTool(workspace))
    registry.register_tool(WriteTool(workspace))
    registry.register_tool(PatchTool(workspace))
    registry.register_tool(GlobTool(workspace))
    registry.register_tool(GrepTool(workspace))
    if skills is not None:
        registry.register_tool(ReadSkillTool(skills))
        registry.register_tool(ReadSkillResourceTool(skills))
    for extra in extra_tools:
        registry.register_tool(extra)
    return registry
