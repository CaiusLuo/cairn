import asyncio
from pathlib import Path

from cairn.core.models import ToolResult
from cairn.infra.tools.lease import LeaseTool
from cairn.infra.workspace.lease import WorkspaceLease


async def _noop(arguments: dict[str, object], *, context=None) -> ToolResult:  # type: ignore[no-untyped-def]
    return ToolResult(exit_code=0, stdout="ok")


class FakeTool:
    name = "write_file"
    description = "fake write"

    def schema(self) -> dict[str, object]:
        return {"name": self.name}

    def validate(self, arguments: dict[str, object]) -> None:
        return None

    async def execute(
        self, arguments: dict[str, object], *, context=None
    ) -> ToolResult:  # type: ignore[no-untyped-def]
        return await _noop(arguments, context=context)


def test_workspace_lease_excludes_second_owner_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "project-write.lock"
    first = WorkspaceLease(path)
    second = WorkspaceLease(path)
    assert first.acquire() is True
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_lease_tool_returns_project_busy_without_delegating(tmp_path: Path) -> None:
    lease = WorkspaceLease(tmp_path / "project-write.lock")
    holder = WorkspaceLease(tmp_path / "project-write.lock")
    assert holder.acquire() is True
    tool = LeaseTool(FakeTool(), lease)
    result = asyncio.run(tool.execute({}))
    assert result.exit_code == 1
    assert result.error_type == "project_busy"
    holder.release()
    result = asyncio.run(tool.execute({}))
    assert result.exit_code == 0
