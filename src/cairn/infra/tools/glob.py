"""Workspace file-name search."""

from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.arguments import function_schema, reject_unknown_keys
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.workspace.paths import (
    contained_candidate,
    validate_workspace_pattern,
)
from cairn.infra.workspace.workspace import Workspace

DEFAULT_LIMIT = 100

# Bound the number of accepted matches a single search may collect, so a pattern
# matching a huge tree stops early instead of materializing every path. The
# iteration itself is bounded by pathlib's walk, not by this constant.
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
        return function_schema(
            self.name,
            self.description,
            {
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
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        """Check the glob input contract before the search runs.

        The pattern's literal prefix is compared against the Workspace, which
        reads directory metadata to reject a symlinked component; it performs no
        write and grants no authority.
        """
        reject_unknown_keys(arguments, {"pattern", "limit"}, tool_label="glob tool")

        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip() or "\0" in pattern:
            raise InvalidArguments("pattern must be a non-empty string without NUL")

        # A pattern naming only the root or a separator is not a search: pathlib
        # raises IndexError or AttributeError from inside glob() instead of
        # reporting a result, so reject it as an argument error here. A component
        # of "...", like any other real name, is a legitimate pattern.
        if all(part in ("", ".", "..") for part in pattern.split("/")):
            raise InvalidArguments("pattern must select a name below the workspace")

        # First containment layer: a pattern that can leave the Workspace is an
        # argument error, so the model is told the pattern is refused instead of
        # receiving an empty "no matches" result. An absolute pattern already
        # broke pathlib with NotImplementedError; this makes it a tool failure.
        try:
            validate_workspace_pattern(self.workspace.root, pattern)
        except ValueError as exc:
            raise InvalidArguments(str(exc)) from exc

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
        root = self.workspace.root
        # Per-directory containment answers, so a wide scan does not re-walk the
        # same parent components for every candidate.
        contained_directories: dict[Path, Path | None] = {}

        for candidate in root.glob(pattern):
            if len(matches) > MAX_SCAN_CANDIDATES:
                scan_exhausted = False
                break

            # Second containment layer: check the RESOLVED candidate, not the
            # lexical one. A pattern is not a path, so validate() can only check
            # its literal prefix; this drops anything an actual match reached
            # outside the Workspace (for example through a symlinked directory
            # named by the pattern) or into .git.
            contained = contained_candidate(
                root, candidate, directory_cache=contained_directories
            )
            if contained is None:
                continue

            if contained.is_symlink() or not contained.is_file():
                continue

            matches.append(contained.relative_to(root).as_posix())

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
