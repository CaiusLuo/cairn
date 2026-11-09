"""Explicit built-in tool assembly; execution and approval stay with runtime."""

from collections.abc import Sequence

from cairn.agent.skills import SkillLibrary
from cairn.agent.tools.base import Tool
from cairn.agent.tools.bash import BashTool
from cairn.agent.tools.files import EditFileTool, ReadFileTool
from cairn.agent.tools.glob import GlobTool
from cairn.agent.tools.grep import GrepTool
from cairn.agent.tools.patch import PatchTool
from cairn.agent.tools.registry import ToolRegistry
from cairn.agent.tools.skill import ReadSkillResourceTool, ReadSkillTool
from cairn.agent.tools.write import WriteTool
from cairn.agent.workspace.workspace import Workspace


def build_default_registry(
    workspace: Workspace,
    *,
    secret_env_keys: frozenset[str] = frozenset(),
    skills: SkillLibrary | None = None,
    extra_tools: Sequence[Tool] = (),
    read_only: bool = False,
    sandbox_shell: bool = False,
) -> ToolRegistry:
    """Create a fresh registry in model-visible declaration order.

    All tools share the exact Workspace; Bash receives the session credential
    policy. A supplied ``skills`` library adds the on-demand reader, and its
    advertisements come from the caller that injects the system message. Callers
    can instrument registered tools with ``override_tool``. Construction grants
    no capabilities and performs no permission decisions.
    """
    if read_only:
        # A read-only conversation gets an empty registry: no shell, file
        # mutation, read/grep crawling, MCP, skill resource or extra tool can
        # become a side channel. Facts come from the bounded snapshot.
        return ToolRegistry()
    registry = ToolRegistry()
    if not read_only:
        registry.register_tool(
            BashTool(
                workspace,
                secret_env_keys=secret_env_keys,
                sandbox_shell=sandbox_shell,
            )
        )
    registry.register_tool(ReadFileTool(workspace))
    if not read_only:
        registry.register_tool(EditFileTool(workspace))
        registry.register_tool(WriteTool(workspace))
        registry.register_tool(PatchTool(workspace))
    registry.register_tool(GlobTool(workspace))
    registry.register_tool(GrepTool(workspace))
    if not read_only:
        if skills is not None:
            registry.register_tool(ReadSkillTool(skills))
            registry.register_tool(ReadSkillResourceTool(skills))
        for extra in extra_tools:
            registry.register_tool(extra)
    return registry
