"""File-name search over the Workspace or a named directory.

The search root defaults to the Workspace. A caller may name any readable
directory; credential material is reported as blocked rather than returned, so
the model learns it exists and can ask the operator instead of silently seeing
nothing.
"""

from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.credential_paths import is_credential_path
from cairn.infra.tools.arguments import function_schema, reject_unknown_keys
from cairn.infra.tools.base import (
    InvalidArguments,
    ToolExecutionContext,
    WorkspaceOnlyTool,
)
from cairn.infra.workspace.paths import (
    contained_candidate,
    has_git_metadata_component,
    resolve_tool_path,
    validate_workspace_pattern,
)
from cairn.infra.workspace.workspace import Workspace

DEFAULT_LIMIT = 100

# Bound the number of accepted matches a single search may collect, so a pattern
# matching a huge tree stops early instead of materializing every path. The
# iteration itself is bounded by pathlib's walk, not by this constant.
MAX_SCAN_CANDIDATES = 50_000


class GlobTool(WorkspaceOnlyTool):
    name = "glob"
    description = (
        "Find files whose paths match a glob pattern, such as '**/*.py'. "
        "Searches the Workspace unless `path` names another directory. "
        "Credential files are reported as blocked, not returned."
    )

    def __init__(
        self, workspace: Workspace, *, max_matches: int = MAX_SCAN_CANDIDATES
    ) -> None:
        self.workspace = workspace
        # Bound the accepted matches this search may collect. Injectable so a
        # test can prove the limit without materializing a 50k-file tree; the
        # default is the production bound.
        self.max_matches = max_matches

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
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute directory to search instead of the "
                            "Workspace root. Must already exist."
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
        reject_unknown_keys(
            arguments, {"pattern", "path", "limit"}, tool_label="glob tool"
        )

        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip() or "\0" in pattern:
            raise InvalidArguments("pattern must be a non-empty string without NUL")

        # A pattern naming only the root or a separator is not a search: pathlib
        # raises IndexError or AttributeError from inside glob() instead of
        # reporting a result, so reject it as an argument error here. A component
        # of "...", like any other real name, is a legitimate pattern.
        if all(part in ("", ".", "..") for part in pattern.split("/")):
            raise InvalidArguments("pattern must select a name below the workspace")

        # A pattern is always relative to the search root: it may not be
        # absolute, ascend, or name .git, whichever root the caller chose.
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise InvalidArguments("pattern must stay inside the search root")
        if any(part.casefold() == ".git" for part in Path(pattern).parts):
            raise InvalidArguments("pattern must stay outside .git")

        raw_path = arguments.get("path")
        if raw_path is not None:
            self._resolve_root(raw_path)
        else:
            # Without an explicit root the search stays in the Workspace, so the
            # stricter prefix rule still applies.
            try:
                validate_workspace_pattern(self.workspace.root, pattern)
            except ValueError as exc:
                raise InvalidArguments(str(exc)) from exc

        limit = arguments.get("limit", DEFAULT_LIMIT)
        if type(limit) is not int or limit < 1:
            raise InvalidArguments("limit must be an integer of at least 1")

    def _resolve_root(self, raw_path: object) -> Path:
        """The directory a caller named as the search root.

        Reading outside the Workspace needs no grant, so an existence check is
        enough; ``.git`` and a non-directory are refused as argument errors.
        """
        try:
            resolved, _ = resolve_tool_path(
                self.workspace.root, raw_path, writable=False
            )
        except ValueError as exc:
            raise InvalidArguments(str(exc)) from exc
        if not resolved.is_dir():
            raise InvalidArguments("path must name a directory")
        return resolved

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate(arguments)
        pattern = arguments["pattern"]
        limit = arguments.get("limit", DEFAULT_LIMIT)
        raw_path = arguments.get("path")
        root = self.workspace.root if raw_path is None else self._resolve_root(raw_path)
        return self._search(root, pattern, limit)

    def _search(self, root: Path, pattern: str, limit: int) -> ToolResult:
        # Directories are deliberately omitted: a missing directory is reported
        # as "no matches" rather than as a path the caller could descend into.
        matches: list[str] = []
        blocked: list[str] = []
        scan_exhausted = True
        # Containment applies whenever the search root sits inside the Workspace,
        # not only when it IS the Workspace. A narrower root is still a view of
        # the Workspace, so it must obey the same rule; otherwise naming a
        # subdirectory would be a way to step around containment for every
        # candidate beneath it.
        inside = root.is_relative_to(self.workspace.root)
        # Per-directory containment answers, so a wide scan does not re-walk the
        # same parent components for every candidate.
        contained_directories: dict[Path, Path | None] = {}

        for candidate in root.glob(pattern):
            if len(matches) + len(blocked) > self.max_matches:
                scan_exhausted = False
                break

            # Git metadata is never a search result, whichever root was named.
            # An outside root has no containment rule to enforce, so this check
            # cannot live only in the containment layer below.
            if has_git_metadata_component(candidate.relative_to(root)):
                continue

            if inside:
                # Second containment layer: check the RESOLVED candidate, not the
                # lexical one. A pattern is not a path, so validate() can only
                # check its literal prefix; this drops anything an actual match
                # reached outside the Workspace or into .git.
                contained = contained_candidate(
                    self.workspace.root,
                    candidate,
                    directory_cache=contained_directories,
                )
                if contained is None:
                    continue
                if contained.is_symlink() or not contained.is_file():
                    continue
                path = contained
            else:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                path = candidate

            relative = path.relative_to(root).as_posix()
            if is_credential_path(path):
                # A credential is reported, not returned: the model learns it
                # exists and can ask the operator instead of seeing nothing.
                blocked.append(relative)
                continue
            matches.append(relative)

        # os.scandir order is arbitrary; sorting makes the result (and every
        # test over it) deterministic.
        matches.sort()
        blocked.sort()

        truncated = not scan_exhausted or len(matches) > limit
        reported = matches[:limit]
        return ToolResult(
            stdout=self._render(root, pattern, reported, blocked),
            exit_code=0,
            stdout_truncated=truncated,
            error_type="permission_required" if blocked else None,
            blocked_paths=tuple(blocked),
        )

    def _render(
        self,
        root: Path,
        pattern: str,
        reported: list[str],
        blocked: list[str],
    ) -> str:
        """Search results, then the credential paths the caller may not read."""
        scope = "workspace" if root == self.workspace.root else str(root)
        lines = list(reported) if reported else [f"No {scope} files match '{pattern}'."]
        if blocked:
            lines.append("")
            lines.append(
                "Blocked: credential files require approval (ask the operator to "
                "grant read access):"
            )
            lines.extend(f"  {path}" for path in blocked[:DEFAULT_LIMIT])
        return "\n".join(lines)
