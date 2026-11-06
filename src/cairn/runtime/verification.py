"""Deterministic file/content checks used by host workflow verification."""

from __future__ import annotations

import asyncio
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from cairn.infra.workspace.workspace import Workspace

CHECK_READ_CHUNK_SIZE = 16 * 1024


class CheckFailureCategory(StrEnum):
    RUN_TIMEOUT = "run_timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RUNTIME_ERROR = "runtime_error"
    CHECK_FAILED = "check_failed"
    CHECK_TIMEOUT = "check_timeout"
    CHECK_ERROR = "check_error"


class CheckResult(BaseModel):
    name: str
    passed: bool
    message: str | None = None
    error: str | None = None
    # One-based position within the case, independent of duplicate check names.
    check_index: int | None = Field(default=None, ge=1)
    check_type: str | None = None
    target_path: str | None = None
    failure_category: CheckFailureCategory | None = None


class Check(Protocol):
    name: str

    async def evaluate(self, workspace: Workspace) -> CheckResult: ...


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
            if not expected.startswith(chunk, offset):
                return False
            offset += len(chunk)


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
