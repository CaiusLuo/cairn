"""Tool wrapper that asks a host-private WorkspaceLease before mutating tools run."""

from __future__ import annotations

from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.base import Tool, ToolExecutionContext
from cairn.infra.workspace.lease import WorkspaceLease

_READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "read_skill",
        "read_skill_resource",
        "glob",
        "grep",
        "memory_search",
        "task_list",
        "task_detail",
    }
)


def _needs_lease(name: str) -> bool:
    return name not in _READ_ONLY_TOOLS


class LeaseTool:
    """Delegate to a tool only while the project write lease is available."""

    def __init__(self, delegate: Tool, lease: WorkspaceLease) -> None:
        self._delegate = delegate
        self._lease = lease
        self.name = delegate.name
        self.description = delegate.description
        self.requested_capability = getattr(delegate, "requested_capability", None)

    def schema(self) -> dict[str, Any]:
        return self._delegate.schema()

    def validate(self, arguments: dict[str, Any]) -> None:
        self._delegate.validate(arguments)

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        if not _needs_lease(self.name):
            return await self._delegate.execute(arguments, context=context)
        if not self._lease.acquire():
            return ToolResult(
                exit_code=1,
                stderr=(
                    "project_busy: another Cairn worker owns the workspace write "
                    "lease; retry after it finishes."
                ),
                error_type="project_busy",
            )
        try:
            return await self._delegate.execute(arguments, context=context)
        finally:
            self._lease.release()
