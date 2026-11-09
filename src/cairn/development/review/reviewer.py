"""Fresh, read-only review through the existing Agent and run_turn lifecycle."""

import asyncio
import json
import math
from typing import Any

from pydantic import ValidationError

from cairn.agent.budget import RunBudget, RunBudgetExceeded
from cairn.agent.context.window import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextBuilder,
)
from cairn.agent.harness import Agent
from cairn.agent.models.client import LLMClient
from cairn.agent.prompts import system_prompt_for
from cairn.agent.tools.files import ReadFileTool
from cairn.agent.tools.registry import ToolRegistry
from cairn.agent.tracing.models import Span
from cairn.agent.tracing.tracer import Tracer
from cairn.agent.turn.events import Event, EventHandler
from cairn.agent.turn.loop import run_turn
from cairn.development.execution.models import TaskSpec
from cairn.development.review.git import (
    ReviewInput,
    ReviewInputError,
    assert_review_unchanged,
    prepare_review,
)
from cairn.development.review.models import ReviewResult, ReviewStatus, _ReviewResponse
from cairn.development.workflow.git import SnapshotDriftError, WorkflowGit
from cairn.development.workflow.models import GitSnapshot, VerificationResult
from cairn.shared.lifecycle import await_owned, settle_owned

MAX_REVIEW_OUTPUT_BYTES = 64 * 1024


class _NullTraceSink:
    def emit(self, span: Span) -> None:
        pass


class _MalformedReviewOutput(Exception):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate review JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("Invalid review JSON constant")


def _parse_response(output: str, snapshot: GitSnapshot) -> _ReviewResponse:
    try:
        # The character check bounds the temporary UTF-8 encoding allocation.
        if (
            len(output) > MAX_REVIEW_OUTPUT_BYTES
            or len(output.encode("utf-8")) > MAX_REVIEW_OUTPUT_BYTES
        ):
            raise ValueError("Review output exceeds the byte limit")
        # Pydantic accepts duplicate JSON keys. Check all objects before strict
        # JSON-mode validation, which correctly admits string enum values.
        json.loads(
            output,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        response = _ReviewResponse.model_validate_json(output)
        if response.reviewed_tree != snapshot.tree_revision or any(
            finding.path not in snapshot.changed_files for finding in response.findings
        ):
            raise ValueError("Review output does not describe the staged proposal")
    except (ValueError, ValidationError):
        raise _MalformedReviewOutput from None
    return response


def _review_prompt(
    task: TaskSpec,
    snapshot: GitSnapshot,
    verification: VerificationResult,
    diff: str,
) -> str:
    return json.dumps(
        {
            "trusted_repository_facts": {
                "branch": snapshot.branch,
                "head_revision": snapshot.head_revision,
                "tree_revision": snapshot.tree_revision,
                "changed_files": snapshot.changed_files,
                "verification_checks": [
                    {"check_id": check.check_id, "passed": check.passed}
                    for check in verification.checks
                ],
            },
            "untrusted_original_task": task.prompt,
            "untrusted_exact_diff": diff,
        },
        ensure_ascii=True,
    )


class FreshReviewer:
    """Create a fresh restricted Agent for each independently verified review.

    The owned lifecycle and final drift check always settle before external
    cancellation propagates, including repeated caller cancellation.
    """

    def __init__(
        self,
        git: WorkflowGit,
        llm: LLMClient,
        *,
        budget: RunBudget,
        timeout_seconds: float,
        context_budget: ContextBudget | None = None,
        event_handler: EventHandler | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Review timeout must be a positive finite number")
        self.git = git
        self.llm = llm
        self.budget = budget
        self.timeout_seconds = timeout_seconds
        self.context_budget = context_budget
        self.event_handler = event_handler
        self.tracer = tracer if tracer is not None else Tracer(_NullTraceSink())

    async def review(
        self,
        task: TaskSpec,
        snapshot: GitSnapshot,
        verification: VerificationResult | None,
    ) -> ReviewResult:
        worker = asyncio.create_task(self._execute(task, snapshot, verification))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as primary:
            caller = asyncio.current_task()
            caller_cancelled = caller is not None and caller.cancelling() > 0
            await settle_owned(worker)
            try:
                worker.result()
            except asyncio.CancelledError as exc:
                if not caller_cancelled:
                    raise exc
            except Exception:
                primary.add_note("Review cleanup failed after cancellation.")
            raise primary

    async def _check_unchanged(
        self, snapshot: GitSnapshot, review_input: ReviewInput
    ) -> None:
        async with asyncio.timeout(self.timeout_seconds):
            await assert_review_unchanged(
                self.git, snapshot, review_input.workspace_state
            )

    async def _execute(
        self,
        task: TaskSpec,
        snapshot: GitSnapshot,
        verification: VerificationResult | None,
    ) -> ReviewResult:
        trace_id: str | None = None
        review_input: ReviewInput | None = None
        response: _ReviewResponse | None = None
        status = ReviewStatus.RUNTIME_ERROR
        cancellation: asyncio.CancelledError | None = None

        def forward_event(event: Event) -> None:
            nonlocal trace_id
            if event.type in ("trace_start", "trace_finish"):
                trace_id = event.data["trace_id"]
            if self.event_handler is not None:
                self.event_handler(event)

        try:
            async with asyncio.timeout(self.timeout_seconds):
                review_input = await prepare_review(self.git, snapshot, verification)
                # prepare_review has validated that the typed checks pass for
                # this exact tree before any model request can be constructed.
                assert verification is not None
                tools = ToolRegistry()
                tools.register_tool(ReadFileTool(self.git.handle.workspace))
                agent = Agent(
                    self.llm,
                    tools,
                    system_prompt=system_prompt_for("review"),
                    context_builder=ContextBuilder(budget=self.context_budget),
                    event_handler=forward_event,
                    tracer=self.tracer,
                )
                output = await run_turn(
                    agent,
                    _review_prompt(task, snapshot, verification, review_input.diff),
                    budget=self.budget,
                )
                response = _parse_response(output, snapshot)
                status = (
                    ReviewStatus.COMPLETED
                    if response.complete
                    else ReviewStatus.INCOMPLETE
                )
        except ReviewInputError as exc:
            status = exc.status
        except SnapshotDriftError:
            status = ReviewStatus.SNAPSHOT_DRIFT
        except (RunBudgetExceeded, ContextBudgetExceeded):
            status = ReviewStatus.BUDGET_EXHAUSTED
        except TimeoutError:
            status = ReviewStatus.TIMED_OUT
        except _MalformedReviewOutput:
            status = ReviewStatus.MALFORMED_OUTPUT
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            status = ReviewStatus.RUNTIME_ERROR
        finally:
            if review_input is not None:
                cleanup = asyncio.create_task(
                    self._check_unchanged(snapshot, review_input)
                )
                # The unchanged-snapshot check *is* the cleanup, so it must be
                # awaited to completion rather than cancelled with the work.
                observed = await await_owned(cleanup)
                if cancellation is None:
                    cancellation = observed
                try:
                    cleanup.result()
                except SnapshotDriftError:
                    status = ReviewStatus.SNAPSHOT_DRIFT
                except TimeoutError:
                    status = ReviewStatus.TIMED_OUT
                except asyncio.CancelledError as exc:
                    if cancellation is None:
                        cancellation = exc
                except Exception:
                    status = ReviewStatus.RUNTIME_ERROR

        if cancellation is not None:
            raise cancellation
        return ReviewResult(
            status=status,
            reviewed_tree=snapshot.tree_revision,
            trace_id=trace_id,
            findings=(
                response.findings
                if response is not None
                and status in (ReviewStatus.COMPLETED, ReviewStatus.INCOMPLETE)
                else ()
            ),
        )
