"""Explicit deterministic verification, independent of model claims."""

import asyncio
import math
from collections.abc import Sequence
from typing import Protocol

from cairn.evals.models import CheckResult, EvalCheck
from cairn.workflow.models import (
    GitSnapshot,
    VerificationCategory,
    VerificationCheck,
    VerificationResult,
)
from cairn.workspace.workspace import Workspace


class DeterministicVerifier(Protocol):
    async def verify(
        self, workspace: Workspace, snapshot: GitSnapshot
    ) -> VerificationResult: ...


class FixedChecksVerifier:
    """Caller-selected checks, with no Issue-controlled commands or expectations."""

    def __init__(
        self, checks: Sequence[EvalCheck], *, timeout_seconds: float = 30.0
    ) -> None:
        if not checks:
            raise ValueError("At least one fixed check is required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Check timeout must be finite and positive")
        self.checks = tuple(checks)
        self.timeout_seconds = timeout_seconds

    async def verify(
        self, workspace: Workspace, snapshot: GitSnapshot
    ) -> VerificationResult:
        results: list[VerificationCheck] = []
        for position, check in enumerate(self.checks, 1):
            # IDs do not include names, paths, expected values, or result text.
            kind = type(check).__name__
            kind = kind if kind.isascii() and kind.isidentifier() else "EvalCheck"
            check_id = f"{position}:{kind[:70]}"
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    result = await check.evaluate(workspace)
                if (
                    not isinstance(result, CheckResult)
                    or type(result.passed) is not bool
                ):
                    raise ValueError("Malformed fixed check result")
                passed = result.passed and result.error is None
                category = None if passed else VerificationCategory.CHECK_FAILED
                if result.error is not None:
                    category = VerificationCategory.CHECK_ERROR
            except TimeoutError:
                passed, category = False, VerificationCategory.CHECK_TIMEOUT
            except Exception:
                passed, category = False, VerificationCategory.CHECK_ERROR
            results.append(VerificationCheck(check_id, passed, category))
        return VerificationResult(snapshot.tree_revision, tuple(results))
