import asyncio
import codecs
import errno
import math
import os
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from cairn.core.models import ToolResult
from cairn.infra.tools.arguments import (
    function_schema,
    reject_unknown_keys,
    workspace_path,
)
from cairn.infra.tools.base import (
    InvalidArguments,
    ToolExecutionContext,
    ToolExecutionFailure,
    ToolFailureKind,
    ToolFailureOutcome,
)
from cairn.infra.workspace.workspace import Workspace

READ_FILE_MAX_LINES = 200
READ_FILE_DISPLAY_CHAR_LIMIT = 12_000

# UTF-8 最多 4 bytes / character。
# 这样仍然允许旧 contract 最多展示 12k Unicode 字符。
READ_FILE_CAPTURE_BYTE_LIMIT = READ_FILE_DISPLAY_CHAR_LIMIT * 4

READ_FILE_CHUNK_SIZE = 4 * 1024

EDIT_FILE_MAX_BYTES = 1024 * 1024  # 1 MiB

READ_FILE_MAX_ATTEMPTS = 3
READ_FILE_RETRY_DEADLINE_SECONDS = 0.25
READ_FILE_RETRY_BUDGET_SECONDS = 0.03
READ_FILE_INITIAL_BACKOFF_SECONDS = 0.01
READ_FILE_MAX_BACKOFF_SECONDS = 0.02

_TRANSIENT_READ_ERRNOS = frozenset(
    value
    for value in (
        errno.EAGAIN,
        errno.EINTR,
        getattr(errno, "EBUSY", None),
        getattr(errno, "ESTALE", None),
    )
    if value is not None
)


@dataclass(frozen=True, slots=True)
class ReadFileRetryPolicy:
    """Fixed bounds for the built-in read-only file operation.

    The retry budget limits cumulative backoff independently from the attempt
    count and the single deadline shared by every attempt. This policy is not a
    general tool retry contract.
    """

    max_attempts: int = READ_FILE_MAX_ATTEMPTS
    deadline_seconds: float = READ_FILE_RETRY_DEADLINE_SECONDS
    retry_budget_seconds: float = READ_FILE_RETRY_BUDGET_SECONDS
    initial_backoff_seconds: float = READ_FILE_INITIAL_BACKOFF_SECONDS
    max_backoff_seconds: float = READ_FILE_MAX_BACKOFF_SECONDS

    def __post_init__(self) -> None:
        numeric = (
            self.deadline_seconds,
            self.retry_budget_seconds,
            self.initial_backoff_seconds,
            self.max_backoff_seconds,
        )
        if (
            type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= READ_FILE_MAX_ATTEMPTS
        ):
            raise ValueError(
                f"read_file max_attempts must be between 1 and {READ_FILE_MAX_ATTEMPTS}"
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in numeric
        ):
            raise ValueError("read_file retry limits must be finite numbers")
        if self.deadline_seconds <= 0:
            raise ValueError("read_file retry deadline must be positive")
        if self.deadline_seconds > READ_FILE_RETRY_DEADLINE_SECONDS:
            raise ValueError("read_file retry deadline exceeds the safety cap")
        if self.retry_budget_seconds < 0:
            raise ValueError("read_file retry budget must be non-negative")
        if self.retry_budget_seconds > READ_FILE_RETRY_BUDGET_SECONDS:
            raise ValueError("read_file retry budget exceeds the safety cap")
        if self.initial_backoff_seconds <= 0 or self.max_backoff_seconds <= 0:
            raise ValueError("read_file retry backoff must be positive")
        if self.max_backoff_seconds > READ_FILE_MAX_BACKOFF_SECONDS:
            raise ValueError("read_file retry backoff exceeds the safety cap")
        if self.initial_backoff_seconds > self.max_backoff_seconds:
            raise ValueError("read_file initial backoff must not exceed its cap")


DEFAULT_READ_FILE_RETRY_POLICY = ReadFileRetryPolicy()


def _iter_bounded_line_fragments(
    file: BinaryIO,
    *,
    check_deadline: Callable[[], None] | None = None,
) -> Iterator[tuple[bytes, bool]]:
    pending_carriage_return = False

    while True:
        if check_deadline is not None:
            check_deadline()
        chunk = file.readline(READ_FILE_CHUNK_SIZE)
        if check_deadline is not None:
            check_deadline()
        if not chunk:
            if pending_carriage_return:
                yield b"\r", True
            return

        index = 0
        if pending_carriage_return:
            if chunk.startswith(b"\n"):
                yield b"\r\n", True
                index = 1
            else:
                yield b"\r", True
            pending_carriage_return = False

        fragment_start = index
        # Binary readline leaves any LF at the end; only CR needs scanning.
        if chunk.find(b"\r", index) < 0:
            if index < len(chunk):
                fragment = chunk[index:]
                yield fragment, fragment.endswith(b"\n")
            continue

        while index < len(chunk):
            byte = chunk[index]
            if byte == ord("\r"):
                if fragment_start < index:
                    yield chunk[fragment_start:index], False
                if index + 1 == len(chunk):
                    pending_carriage_return = True
                    index += 1
                elif chunk[index + 1] == ord("\n"):
                    yield b"\r\n", True
                    index += 2
                else:
                    yield b"\r", True
                    index += 1
                fragment_start = index
            elif byte == ord("\n"):
                yield chunk[fragment_start : index + 1], True
                index += 1
                fragment_start = index
            else:
                index += 1

        if fragment_start < len(chunk):
            yield chunk[fragment_start:], False


def _read_bounded_range(
    file: BinaryIO,
    *,
    start_line: int,
    end_line: int,
    capture_limit: int = READ_FILE_CAPTURE_BYTE_LIMIT,
    check_deadline: Callable[[], None] | None = None,
) -> tuple[bytes, bool, bool]:
    captured = bytearray()
    line_number = 1
    truncated = False
    has_more = False

    fragments = iter(_iter_bounded_line_fragments(file, check_deadline=check_deadline))
    for fragment, line_complete in fragments:
        if check_deadline is not None:
            check_deadline()
        if start_line <= line_number <= end_line:
            remaining = capture_limit - len(captured)
            if remaining > 0:
                captured.extend(fragment[:remaining])
            if len(fragment) > remaining:
                truncated = True
                break

        if line_complete:
            if line_number >= end_line:
                has_more = next(fragments, None) is not None
                break
            line_number += 1

    return bytes(captured), truncated, has_more


def _read_edit_file(path: Path) -> str:
    with path.open("rb") as file:
        data = file.read(EDIT_FILE_MAX_BYTES + 1)

    if len(data) > EDIT_FILE_MAX_BYTES:
        raise ValueError(
            f"File exceeds edit_file limit of {EDIT_FILE_MAX_BYTES} bytes; "
            "no changes made"
        )

    return data.decode("utf-8")


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _decode_bounded_utf8(data: bytes, *, truncated: bool) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    return decoder.decode(data, final=not truncated)


class ReadFileTool:
    name = "read_file"
    description = "Read up to 200 lines of a UTF-8 workspace file."

    def __init__(
        self,
        workspace: Workspace,
        *,
        retry_policy: ReadFileRetryPolicy | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.workspace = workspace
        self.retry_policy = (
            retry_policy if retry_policy is not None else DEFAULT_READ_FILE_RETRY_POLICY
        )
        self._monotonic = monotonic
        self._sleep = sleep

    def schema(self) -> dict[str, Any]:
        return function_schema(
            self.name,
            self.description,
            {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path.",
                    },
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        reject_unknown_keys(
            arguments,
            {"path", "start_line", "end_line"},
            tool_label="file tool",
        )
        workspace_path(self.workspace, arguments)
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line", start + 199 if isinstance(start, int) else 0)
        if (
            type(start) is not int
            or type(end) is not int
            or start < 1
            or end < start
            or end - start >= READ_FILE_MAX_LINES
        ):
            raise InvalidArguments("read_file requires a range of 1 to 200 lines")

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate(arguments)
        deadline = self._monotonic() + self.retry_policy.deadline_seconds
        backoff_used = 0.0

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                return self._read_once(arguments, deadline=deadline)
            except OSError as error:
                outcome = self._classify_os_error(error)
                if outcome.kind is not ToolFailureKind.TRANSIENT_BEFORE_EFFECT:
                    raise
                if attempt >= self.retry_policy.max_attempts:
                    raise ToolExecutionFailure(outcome) from None

                delay = min(
                    self.retry_policy.initial_backoff_seconds * 2 ** (attempt - 1),
                    self.retry_policy.max_backoff_seconds,
                )
                now = self._monotonic()
                if now + delay > deadline:
                    raise ToolExecutionFailure(self._deadline_outcome()) from None
                if backoff_used + delay > self.retry_policy.retry_budget_seconds:
                    raise ToolExecutionFailure(outcome) from None

                await self._sleep(delay)
                backoff_used += delay

        raise AssertionError("read_file retry loop exhausted without an outcome")

    def _read_once(self, arguments: dict[str, Any], *, deadline: float) -> ToolResult:
        self._check_deadline(deadline)
        path = self.workspace.resolve_path(arguments["path"])
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line", start + 199)
        is_file = path.is_file()
        self._check_deadline(deadline)
        if not is_file:
            raise FileNotFoundError(f"File not found: {arguments['path']}")

        with path.open("rb") as file:
            self._check_deadline(deadline)
            data, byte_truncated, has_more = _read_bounded_range(
                file,
                start_line=start,
                end_line=end,
                check_deadline=lambda: self._check_deadline(deadline),
            )

        self._check_deadline(deadline)
        content = _decode_bounded_utf8(data, truncated=byte_truncated)

        display_truncated = len(content) > READ_FILE_DISPLAY_CHAR_LIMIT
        if display_truncated:
            content = content[:READ_FILE_DISPLAY_CHAR_LIMIT]

        truncated = byte_truncated or display_truncated

        if truncated:
            content += "\n[read_file output truncated]"
        elif has_more:
            content += "\n[read_file more lines available]"

        return ToolResult(
            stdout=content,
            exit_code=0,
            stdout_truncated=truncated,
        )

    def _check_deadline(self, deadline: float) -> None:
        if self._monotonic() >= deadline:
            raise ToolExecutionFailure(self._deadline_outcome())

    @staticmethod
    def _deadline_outcome() -> ToolFailureOutcome:
        return ToolFailureOutcome(
            kind=ToolFailureKind.DEADLINE,
            error_type="TimeoutError",
            message="read_file retry deadline exceeded",
        )

    @staticmethod
    def _classify_os_error(error: OSError) -> ToolFailureOutcome:
        if isinstance(error, PermissionError):
            kind = ToolFailureKind.PERMISSION_DENIED
        elif error.errno in _TRANSIENT_READ_ERRNOS:
            kind = ToolFailureKind.TRANSIENT_BEFORE_EFFECT
        else:
            kind = ToolFailureKind.PERMANENT
        return ToolFailureOutcome(
            kind=kind,
            error_type=type(error).__name__,
            message=str(error),
        )


class EditFileTool:
    name = "edit_file"
    description = (
        "Replace exactly one old_text in a UTF-8 workspace file. "
        "Set old_text to empty to create a new file; existing files cannot be overwritten."
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
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path.",
                    },
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Replace every occurrence of old_text instead of "
                            "exactly one. Unsupported when creating a file."
                        ),
                    },
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        reject_unknown_keys(
            arguments,
            {"path", "old_text", "new_text", "replace_all"},
            tool_label="file tool",
        )
        workspace_path(self.workspace, arguments)
        if not isinstance(arguments.get("old_text"), str) or not isinstance(
            arguments.get("new_text"), str
        ):
            raise InvalidArguments("old_text and new_text must be strings")
        if not isinstance(arguments.get("replace_all", False), bool):
            raise InvalidArguments("replace_all must be a boolean")

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.validate(arguments)
        path = self.workspace.resolve_path(arguments["path"])
        old_text = arguments["old_text"]
        new_text = arguments["new_text"]
        replace_all = arguments.get("replace_all", False)

        creating = old_text == ""
        if creating:
            if replace_all:
                raise ValueError("replace_all is not supported when creating a file")
            if path.exists():
                raise ValueError("File already exists; no changes made")
            if _utf8_size(new_text) > EDIT_FILE_MAX_BYTES:
                raise ValueError(
                    f"New file would exceed edit_file limit of "
                    f"{EDIT_FILE_MAX_BYTES} bytes; no changes made"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            updated = new_text
        else:
            if not path.is_file():
                raise FileNotFoundError(f"File not found: {arguments['path']}")
            original = _read_edit_file(path)
            matches = original.count(old_text)
            if matches == 0:
                raise ValueError("old_text matched 0 times; no changes made")
            if matches > 1 and not replace_all:
                raise ValueError(f"old_text matched {matches} times; no changes made")

            updated_size = (
                _utf8_size(original)
                - _utf8_size(old_text) * matches
                + _utf8_size(new_text) * (matches if replace_all else 1)
            )

            if updated_size > EDIT_FILE_MAX_BYTES:
                raise ValueError(
                    f"Edited file would exceed edit_file limit of "
                    f"{EDIT_FILE_MAX_BYTES} bytes; no changes made"
                )

            updated = original.replace(old_text, new_text, -1 if replace_all else 1)

        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cairn-edit-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
                file.write(updated)
            if creating:
                if path.exists():
                    raise ValueError("File already exists; no changes made")
                os.link(temporary, path)
            else:
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
                os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        action = "Created" if creating else "Updated"
        return ToolResult(stdout=f"{action} {arguments['path']}", exit_code=0)
