"""Workspace file-name search."""

from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.workspace.workspace import Workspace

DEFAULT_LIMIT = 100

# Bound the scan itself, not just the reported matches. A pattern that matches
# nothing can otherwise walk an unbounded tree before the caller learns that.
MAX_SCAN_CANDIDATES = 50_000


class GlobTool:
    name = "glob"
    description = (
        "Find workspace files whose paths match a glob pattern, such as "
        "'**/*.py'. Returns workspace-relative paths."
    )

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": (
                                "Glob pattern matched against workspace-relative "
                                "paths, such as '**/*.py' or 'src/*/test_*.py'. "
                                "A leading '**/' matches at any depth."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "default": DEFAULT_LIMIT,
                            "description": (
                                "Maximum number of paths to return. When more "
                                "matches exist, output is marked truncated."
                            ),
                        },
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        """Check the glob input contract without touching the filesystem."""
        if arguments.keys() - {"pattern", "limit"}:
            raise InvalidArguments("unexpected glob tool argument")

        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip() or "\0" in pattern:
            raise InvalidArguments("pattern must be a non-empty string without NUL")

        limit = arguments.get("limit", DEFAULT_LIMIT)
        if type(limit) is not int or limit < 1:
            raise InvalidArguments("limit must be an integer of at least 1")

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate(arguments)
        pattern = arguments["pattern"]
        limit = arguments.get("limit", DEFAULT_LIMIT)
        return self._search(pattern, limit)

    def _search(self, pattern: str, limit: int) -> ToolResult:
        # Directories are deliberately omitted: a missing directory is reported
        # as "no matches" rather than as a path the caller could descend into.
        matches: list[str] = []
        scan_exhausted = True

        for candidate in self.workspace.root.glob(pattern):
            if len(matches) > MAX_SCAN_CANDIDATES:
                scan_exhausted = False
                break

            # Symlinks are rejected everywhere else in the workspace contract,
            # so a symlinked path is not a task result either. pathlib's '**'
            # refuses to descend into a symlinked directory; this drops the case
            # where the *final* component is itself a link.
            if candidate.is_symlink():
                continue

            if not candidate.is_file():
                continue

            if ".git" in candidate.relative_to(self.workspace.root).parts:
                continue

            if candidate.is_relative_to(self.workspace.root):
                matches.append(candidate.relative_to(self.workspace.root).as_posix())

        # os.scandir order is arbitrary; sorting makes the result (and every
        # test over it) deterministic.
        matches.sort()

        truncated = not scan_exhausted or len(matches) > limit
        reported = matches[:limit]

        if not reported:
            return ToolResult(
                stdout=f"No workspace files match '{pattern}'.",
                exit_code=0,
            )

        return ToolResult(
            stdout="\n".join(reported),
            exit_code=0,
            stdout_truncated=truncated,
        )
