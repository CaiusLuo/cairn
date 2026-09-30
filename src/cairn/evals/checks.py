import asyncio
import stat
from dataclasses import dataclass
from pathlib import Path

from cairn.evals.models import CheckResult
from cairn.workspace.workspace import Workspace

CHECK_READ_CHUNK_SIZE = 16 * 1024


def _regular_file(workspace: Workspace, raw_path: str) -> tuple[Path, bool]:
    path = workspace.resolve_path(raw_path)
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return path, False
    return path, stat.S_ISREG(mode)


async def _content_equals(path: Path, expected: str) -> bool:
    offset = 0
    with path.open(encoding="utf-8") as stream:
        while True:
            chunk = stream.read(CHECK_READ_CHUNK_SIZE)
            await asyncio.sleep(0)
            if not chunk:
                return offset == len(expected)
            if chunk != expected[offset : offset + len(chunk)]:
                return False
            offset += len(chunk)


async def _contains(path: Path, substring: str) -> bool:
    overlap = ""
    overlap_size = max(0, len(substring) - 1)
    with path.open(encoding="utf-8") as stream:
        while True:
            chunk = stream.read(CHECK_READ_CHUNK_SIZE)
            await asyncio.sleep(0)
            window = overlap + chunk
            if substring in window:
                return True
            if not chunk:
                return False
            overlap = window[-overlap_size:] if overlap_size else ""


@dataclass(slots=True)
class FileExistsCheck:
    path: str
    name: str = "file_exists"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        _, exists = _regular_file(workspace, self.path)
        message = None if exists else f"Expected a regular file at {self.path}"
        return CheckResult(name=self.name, passed=exists, message=message)


@dataclass(slots=True)
class FileContentEqualsCheck:
    path: str
    expected: str
    name: str = "file_content_equals"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        if not isinstance(self.expected, str):
            raise ValueError("expected content must be a string")
        resolved, is_file = _regular_file(workspace, self.path)
        passed = is_file and await _content_equals(resolved, self.expected)
        message = None if passed else f"Expected {self.path} to equal {self.expected!r}"
        return CheckResult(name=self.name, passed=passed, message=message)


@dataclass(slots=True)
class FileContainsCheck:
    path: str
    substring: str
    name: str = "file_contains"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        if not isinstance(self.substring, str):
            raise ValueError("substring must be a string")
        resolved, is_file = _regular_file(workspace, self.path)
        passed = is_file and await _contains(resolved, self.substring)
        message = (
            None if passed else f"Expected {self.path} to contain {self.substring!r}"
        )
        return CheckResult(name=self.name, passed=passed, message=message)


@dataclass(slots=True)
class FileNotContainsCheck:
    path: str
    substring: str
    name: str = "file_not_contains"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        if not isinstance(self.substring, str):
            raise ValueError("substring must be a string")
        resolved, is_file = _regular_file(workspace, self.path)
        passed = is_file and not await _contains(resolved, self.substring)
        message = (
            None
            if passed
            else f"Expected {self.path} not to contain {self.substring!r}"
        )
        return CheckResult(name=self.name, passed=passed, message=message)
