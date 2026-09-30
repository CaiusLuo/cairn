import stat
from dataclasses import dataclass
from pathlib import Path

from cairn.evals.models import CheckResult
from cairn.workspace.workspace import Workspace


def _regular_file(workspace: Workspace, raw_path: str) -> tuple[Path, bool]:
    path = workspace.resolve_path(raw_path)
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return path, False
    return path, stat.S_ISREG(mode)


def _read_text(workspace: Workspace, raw_path: str) -> tuple[Path, str | None]:
    path, is_file = _regular_file(workspace, raw_path)
    if not is_file:
        return path, None
    return path, path.read_text(encoding="utf-8")


@dataclass(slots=True)
class FileExistsCheck:
    path: str
    name: str = "file_exists"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        resolved, exists = _regular_file(workspace, self.path)
        message = None if exists else f"Expected a regular file at {resolved}"
        return CheckResult(name=self.name, passed=exists, message=message)


@dataclass(slots=True)
class FileContentEqualsCheck:
    path: str
    expected: str
    name: str = "file_content_equals"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        if not isinstance(self.expected, str):
            raise ValueError("expected content must be a string")
        resolved, content = _read_text(workspace, self.path)
        passed = content is not None and content == self.expected
        message = None if passed else f"Expected {resolved} to equal {self.expected!r}"
        return CheckResult(name=self.name, passed=passed, message=message)


@dataclass(slots=True)
class FileContainsCheck:
    path: str
    substring: str
    name: str = "file_contains"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        if not isinstance(self.substring, str):
            raise ValueError("substring must be a string")
        resolved, content = _read_text(workspace, self.path)
        passed = content is not None and self.substring in content
        message = (
            None if passed else f"Expected {resolved} to contain {self.substring!r}"
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
        resolved, content = _read_text(workspace, self.path)
        passed = content is not None and self.substring not in content
        message = (
            None if passed else f"Expected {resolved} not to contain {self.substring!r}"
        )
        return CheckResult(name=self.name, passed=passed, message=message)
