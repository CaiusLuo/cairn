"""Tool wrapper that asks a host-private WorkspaceLease before mutating tools run."""

from __future__ import annotations

from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.base import Tool, ToolExecutionContext
from cairn.infra.workspace.lease import WorkspaceLease

_NON_MUTATING_PREFIXES = (
    "read",
    "glob",
    "grep",
    "memory_",
    "task_",
)


def _needs_lease(name: str) -> bool:
    if name.startswith(_NON_MUTATING_PREFIXES):
        return False
    return not name.startswith(_NON_MUTATING_PREFIXES)


class LeaseTool:
    """Delegate to a tool only while the project write lease is available."""

    def __init__(self, delegate: Tool, lease: WorkspaceLease) -> None:
        self._delegate = delegate
        self._lease = lease
        self.name = delegate.name
        self.description = delegate.description

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
