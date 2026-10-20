"""/compact: the manual entry point to context compaction.

The command must reuse the runtime use case rather than reimplement a reduction,
report what happened in terms a user can act on, and stay unavailable when the
session cannot compact at all.
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from cairn.core.compaction import CompactionStatus
from cairn.runtime.turn.compaction import CompactionOutcome, CompactionStep
from cairn.terminal.commands.compact import (
    CompactionReport,
    CompactionReportKind,
    handle_compact,
)


def outcome(
    step: CompactionStep,
    *,
    status: CompactionStatus | None = None,
    folded: int = 0,
    before: int = 1_000,
    after: int = 400,
) -> CompactionOutcome:
    return CompactionOutcome(
        step=step,
        status=status,
        folded_messages=folded,
        tokens_before=before,
        tokens_after=after,
    )


def run(interaction: Callable[[], Awaitable[None]] | None) -> None:
    if interaction is not None:
        asyncio.run(_await(interaction()))


async def _await(awaitable: Awaitable[None]) -> None:
    await awaitable


class TestAvailability:
    def test_reports_unavailable_when_the_session_cannot_compact(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = handle_compact(None)

        assert result is None
        assert "not available" in capsys.readouterr().out


class TestReporting:
    def test_a_summary_is_reported_as_such(self) -> None:
        report = CompactionReport(outcome(CompactionStep.SUMMARIZED))

        assert report.kind is CompactionReportKind.SUMMARIZED

    def test_folding_is_reported_as_such(self) -> None:
        report = CompactionReport(outcome(CompactionStep.FOLDED, folded=3))

        assert report.kind is CompactionReportKind.FOLDED

    def test_an_admissible_request_reports_nothing_to_do(self) -> None:
        report = CompactionReport(
            CompactionOutcome(
                step=CompactionStep.NOT_NEEDED, tokens_before=100, tokens_after=100
            )
        )

        assert report.kind is CompactionReportKind.NOTHING_TO_DO

    @pytest.mark.parametrize(
        "status", [CompactionStatus.SUMMARIZER_FAILED, CompactionStatus.EXHAUSTED]
    )
    def test_a_degraded_attempt_never_claims_to_have_compacted(
        self, status: CompactionStatus
    ) -> None:
        report = CompactionReport(outcome(CompactionStep.FOLDED, status=status))

        assert report.kind is CompactionReportKind.UNAVAILABLE

    def test_every_outcome_produces_a_nonempty_message(self) -> None:
        for step in CompactionStep:
            assert CompactionReport(outcome(step)).describe().strip()


class TestInteraction:
    def test_awaits_the_bound_compaction_and_prints_its_report(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls: list[int] = []

        async def compact_now() -> CompactionOutcome:
            calls.append(1)
            return outcome(CompactionStep.SUMMARIZED)

        interaction = handle_compact(compact_now)

        assert interaction is not None
        run(interaction)
        assert calls == [1]
        assert "Compacted" in capsys.readouterr().out
