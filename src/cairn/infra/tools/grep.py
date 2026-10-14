"""Workspace content search."""

import codecs
import fnmatch
import re
from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.workspace.workspace import Workspace

DEFAULT_LIMIT = 100
DEFAULT_CONTEXT_LINES = 0

# Two independent ceilings. ``DEFAULT_LIMIT`` caps what a single call reports;
# ``MAX_MATCHES`` caps the scan itself so a pathological pattern over a large
# tree cannot make one call run for ever.
MAX_MATCHES = 1000
MAX_LINE_LENGTH = 2000
MAX_INSPECTION_BYTES = 8 * 1024 * 1024

READ_CHUNK_SIZE = 256 * 1024


class GrepTool:
    name = "grep"
    description = (
        "Search the contents of workspace files with a regular expression. "
        "Returns file paths, line numbers and matching lines."
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
                                "Regular expression to search for. Uses Python "
                                "'re' syntax; set 'literal' for an exact match."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": (
                                "Workspace-relative file or directory to search. "
                                "Defaults to the whole workspace."
                            ),
                        },
                        "include": {
                            "type": "string",
                            "description": (
                                "Glob pattern that a candidate file's name must "
                                "match, such as '*.py' or 'test_*.py'."
                            ),
                        },
                        "literal": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "Treat 'pattern' as a literal string rather "
                                "than a regular expression."
                            ),
                        },
                        "case_sensitive": {
                            "type": "boolean",
                            "default": True,
                            "description": "Match case exactly when set.",
                        },
                        "context_lines": {
                            "type": "integer",
                            "minimum": 0,
                            "default": DEFAULT_CONTEXT_LINES,
                            "description": (
                                "Lines of surrounding context to include on "
                                "each side of a match."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "default": DEFAULT_LIMIT,
                            "description": (
                                "Maximum number of matching lines to return. "
                                "When more matches exist, output is marked "
                                "truncated."
                            ),
                        },
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            },
        }

    _ARGUMENTS = frozenset(
        {
            "pattern",
            "path",
            "include",
            "literal",
            "case_sensitive",
            "context_lines",
            "limit",
        }
    )

    def validate(self, arguments: dict[str, Any]) -> None:
        """Check the grep input contract without touching the filesystem.

        Argument validity is owned by the tool that declares the schema. The
        pattern is compiled here as an input check alone: an uncompilable
        pattern is an ``InvalidArguments`` tool failure, never an authorization
        decision.
        """
        if arguments.keys() - self._ARGUMENTS:
            raise InvalidArguments("unexpected grep tool argument")

        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise InvalidArguments("pattern must be a non-empty string")
        if "\0" in pattern:
            raise InvalidArguments("pattern must not contain NUL")

        raw_path = arguments.get("path")
        if raw_path is not None:
            if not isinstance(raw_path, str) or "\0" in raw_path:
                raise InvalidArguments("path must be a string without NUL")
            try:
                self.workspace.resolve_path(raw_path)
            except ValueError as exc:
                raise InvalidArguments(str(exc)) from exc

        include = arguments.get("include")
        if include is not None and (
            not isinstance(include, str) or not include.strip()
        ):
            raise InvalidArguments("include must be a non-empty glob string")

        literal = arguments.get("literal", False)
        if not isinstance(literal, bool):
            raise InvalidArguments("literal must be a boolean")

        case_sensitive = arguments.get("case_sensitive", True)
        if not isinstance(case_sensitive, bool):
            raise InvalidArguments("case_sensitive must be a boolean")

        context_lines = arguments.get("context_lines", DEFAULT_CONTEXT_LINES)
        if type(context_lines) is not int or context_lines < 0:
            raise InvalidArguments("context_lines must be a non-negative integer")

        limit = arguments.get("limit", DEFAULT_LIMIT)
        if type(limit) is not int or limit < 1:
            raise InvalidArguments("limit must be an integer of at least 1")

        try:
            self._compile(pattern, bool(literal), bool(case_sensitive))
        except re.error as exc:
            raise InvalidArguments(f"invalid regular expression: {exc}") from exc

    def _compile(
        self, pattern: str, literal: bool, case_sensitive: bool
    ) -> re.Pattern[str]:
        expression = re.escape(pattern) if literal else pattern
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.compile(expression, flags)

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate(arguments)
        expression = self._compile(
            arguments["pattern"],
            bool(arguments.get("literal", False)),
            bool(arguments.get("case_sensitive", True)),
        )
        context_lines = arguments.get("context_lines", DEFAULT_CONTEXT_LINES)
        limit = arguments.get("limit", DEFAULT_LIMIT)
        return self._search(
            expression,
            arguments.get("path"),
            arguments.get("include"),
            context_lines,
            limit,
        )

    def _candidate_files(self, raw_path: object, include: str | None) -> list[Path]:
        """Files to inspect, in a deterministic order.

        A missing path is reported as a tool failure describing what was asked
        for, so the caller sees "no such directory" rather than an empty result.
        """
        root = self.workspace.root

        if raw_path is not None:
            base = self.workspace.resolve_path(raw_path)
            if base.is_file():
                candidates = [base]
            elif base.is_dir():
                candidates = [path for path in base.rglob("*") if path.is_file()]
            else:
                raise FileNotFoundError(
                    f"No workspace file or directory matches '{raw_path}'"
                )
        else:
            candidates = [path for path in root.rglob("*") if path.is_file()]

        if include is not None:
            candidates = [
                path for path in candidates if fnmatch.fnmatch(path.name, include)
            ]

        # Symlinks are rejected everywhere else in the workspace contract, so a
        # symlinked path is not a task result either.
        return sorted(
            path
            for path in candidates
            if not path.is_symlink()
            and path.is_relative_to(root)
            and ".git" not in path.relative_to(root).parts
        )

    def _read_lines(self, path: Path) -> list[str] | None:
        """Decode a file as lines, or ``None`` when it must be skipped.

        An undecodable, oversized or unreadable file is skipped rather than
        failing the whole search: one unreadable file must not hide every other
        match in the workspace.
        """
        try:
            if path.stat().st_size > MAX_INSPECTION_BYTES:
                return None

            data = b""
            with path.open("rb") as handle:
                while len(data) <= MAX_INSPECTION_BYTES:
                    chunk = handle.read(READ_CHUNK_SIZE)
                    if not chunk:
                        break
                    data += chunk
                else:
                    return None
        except OSError:
            return None

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        return decoder.decode(data, final=True).splitlines()

    def _search(
        self,
        expression: re.Pattern[str],
        raw_path: object,
        include: object,
        context_lines: int,
        limit: int,
    ) -> ToolResult:
        include_glob = include if isinstance(include, str) else None
        candidates = self._candidate_files(raw_path, include_glob)

        entries: list[str] = []
        truncated = False
        matches = 0

        for candidate in candidates:
            lines = self._read_lines(candidate)
            if lines is None:
                continue

            relative = candidate.relative_to(self.workspace.root).as_posix()

            for number, line in enumerate(lines, start=1):
                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH]

                if not expression.search(line):
                    continue

                matches += 1
                if matches > limit or matches > MAX_MATCHES:
                    truncated = True
                    break

                entries.append(
                    self._format_match(relative, number, line, context_lines, lines)
                )

            if truncated:
                break

        if not entries:
            if not candidates:
                message = "No workspace files are available to search."
            else:
                message = f"No matches for pattern '{expression.pattern}'."
            return ToolResult(stdout=message, exit_code=0)

        report = "\n".join(entries)
        if truncated:
            report += "\n[grep output truncated; match limit reached]"

        return ToolResult(
            stdout=report,
            exit_code=0,
            stdout_truncated=truncated,
        )

    def _format_match(
        self,
        relative: str,
        number: int,
        line: str,
        context_lines: int,
        lines: list[str],
    ) -> str:
        """One match as path:line content, optionally with surrounding context."""
        if context_lines <= 0:
            return f"{relative}:{number}: {line}"

        start = max(1, number - context_lines)
        end = min(len(lines), number + context_lines)

        block: list[str] = []
        for current in range(start, end + 1):
            marker = ":" if current == number else "-"
            content = lines[current - 1]
            if len(content) > MAX_LINE_LENGTH:
                content = content[:MAX_LINE_LENGTH]
            block.append(f"{relative}{marker}{current}{marker} {content}")

        return "\n".join(block)
