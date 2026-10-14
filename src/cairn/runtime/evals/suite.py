import asyncio
from collections.abc import Callable
from pathlib import Path

from cairn.runtime.artifacts import atomic_write_text
from cairn.runtime.evals.models import (
    CheckResult,
    EvalFailureCategory,
    EvalResult,
    EvalStatus,
    EvalSuite,
    EvalSuiteConfig,
    EvalSuiteResult,
)
from cairn.runtime.evals.runner import EvalRunner
from cairn.runtime.lifecycle import settle_owned


def write_suite_report(report: EvalSuiteResult, destination: Path) -> None:
    """Atomically replace an explicit local report, keeping the old file on error.

    No prompts, fixtures, model final text or raw events are report fields.
    Suite results normalize free-text diagnostics before reaching this writer.
    """
    atomic_write_text(destination, report.model_dump_json(indent=2) + "\n")


def _report_result(result: EvalResult) -> EvalResult:
    # Arbitrary LLM/check exception messages can contain credentials or request
    # payloads. Keep structured categories, check identities and trace refs;
    # never copy arbitrary failure text or expected file contents.
    return result.model_copy(
        deep=True,
        update={
            "error": "Case execution failed." if result.error is not None else None,
            "failure_category": result.failure_category
            or (
                EvalFailureCategory.RUNTIME_ERROR if result.error is not None else None
            ),
            "checks": [
                CheckResult(
                    name=check.name,
                    passed=check.passed,
                    message=(
                        f"Deterministic check {'passed' if check.passed else 'failed'}."
                        if check.message is not None
                        else None
                    ),
                    error="Check evaluation failed."
                    if check.error is not None
                    else None,
                    check_index=check.check_index,
                    check_type=check.check_type,
                    target_path=check.target_path,
                    failure_category=check.failure_category,
                )
                for check in result.checks
            ],
        },
    )


class EvalSuiteRunner:
    """Sequential regression orchestration around the existing EvalRunner.

    The case runner still owns each temporary Workspace. A suite owns only its
    observed case task and report destination. Persistence errors stop execution;
    ordinary case failures do not. Reports are snapshots, not a benchmark score.
    """

    def __init__(self, runner: EvalRunner) -> None:
        self.runner = runner

    async def run(
        self,
        suite: EvalSuite,
        *,
        destination: Path,
        on_result: Callable[[EvalResult], None] | None = None,
    ) -> EvalSuiteResult:
        report = EvalSuiteResult(
            suite_name=suite.name,
            case_names=tuple(item.case.name for item in suite.cases),
            config=EvalSuiteConfig(
                run_budget=self.runner.budget,
                context_budget=self.runner.context_budget,
                run_timeout_seconds=self.runner.run_timeout_seconds,
                check_timeout_seconds=self.runner.check_timeout_seconds,
            ),
        )
        write_suite_report(report, destination)
        for item in suite.cases:
            metrics = self.runner.metrics_type()
            report.active_case_name = item.case.name
            report.active_case_metrics = metrics
            task = asyncio.create_task(
                self.runner.run(item.case, checks=item.checks, metrics=metrics)
            )
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError as cancelled:
                primary = cancelled
                caller = asyncio.current_task()
                caller_cancelled = caller is not None and caller.cancelling() > 0
                finished = task.done() and not task.cancelled()
                await settle_owned(task)
                try:
                    result = task.result()
                    if finished:
                        report.results.append(_report_result(result))
                        report.active_case_name = None
                        report.active_case_metrics = None
                except asyncio.CancelledError as exc:
                    if not caller_cancelled:
                        primary = exc
                    else:
                        for note in getattr(exc, "__notes__", ()):
                            primary.add_note(note)
                except Exception:
                    primary.add_note("Eval case cleanup failed.")
                report.state = "interrupted"
                try:
                    write_suite_report(report, destination)
                except Exception as exc:
                    primary.add_note(
                        f"Suite report persistence failed: {type(exc).__name__}"
                    )
                if primary is cancelled:
                    raise
                raise primary from None
            except Exception:
                # Defensive boundary for custom runners; standard EvalRunner
                # already maps ordinary execution/check exceptions into ERROR.
                result = EvalResult(
                    case_name=item.case.name,
                    status=EvalStatus.ERROR,
                    error="Case execution failed.",
                    metrics=metrics,
                    failure_category=EvalFailureCategory.RUNTIME_ERROR,
                )
            report.results.append(_report_result(result))
            report.active_case_name = None
            report.active_case_metrics = None
            if len(report.results) == len(suite.cases):
                report.state = "completed"
            write_suite_report(report, destination)
            if on_result is not None:
                on_result(report.results[-1])
        return report
