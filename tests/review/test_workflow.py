import asyncio
import json
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from cairn.core.budget import RunBudget
from cairn.core.events import Event
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.git import WorktreeProvider, WorktreeState
from cairn.observability.tracer import Tracer
from cairn.review import FreshReviewer, ReviewResult, ReviewSeverity, ReviewStatus
from cairn.review import workflow as workflow_module
from cairn.review.workflow import ReviewWorkflow
from cairn.review.workflow_models import ReviewWorkflowFailure, ReviewWorkflowStatus
from cairn.tasks import CodingTaskRunner, TaskSpec, TaskStatus
from cairn.workflow import (
    GitSnapshot,
    VerificationCheck,
    VerificationResult,
    WorkflowGit,
)
from cairn.workspace.workspace import Workspace
from tests.git.test_security import install_git_recorder, records
from tests.review.test_boundaries import filesystem_state
from tests.review.test_reviewer import TASK, finding, proposal
from tests.support.runtime import FailingLLM, RecordingSink, SequenceLLM
from tests.support.sandbox import require_working_sandbox
from tests.workflow.test_local import edit, git
from tests.workflow.test_local import provider as provider


class TreeReviewLLM:
    def __init__(self, outcomes: list[list[dict[str, Any]] | str]) -> None:
        self.outcomes = outcomes
        self.calls: list[list[Message]] = []

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        self.calls.append([message.model_copy(deep=True) for message in messages])
        prompt = json.loads(messages[-1].content or "")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, str):
            return LLMResponse(content=outcome)
        return LLMResponse(
            content=json.dumps(
                {
                    "reviewed_tree": prompt["trusted_repository_facts"][
                        "tree_revision"
                    ],
                    "complete": True,
                    "findings": outcome,
                }
            )
        )


class RecordingVerifier:
    def __init__(self) -> None:
        self.snapshots: list[GitSnapshot] = []
        self.contents: list[str] = []

    async def verify(
        self, workspace: Workspace, snapshot: GitSnapshot
    ) -> VerificationResult:
        self.snapshots.append(snapshot)
        self.contents.append((workspace.root / "file.txt").read_text())
        return VerificationResult(
            snapshot.tree_revision, (VerificationCheck("fixed", True),)
        )


def fixer(*values: str) -> SequenceLLM:
    responses: list[LLMResponse] = []
    old = "after\n"
    for value in values:
        responses.extend(
            [
                LLMResponse(tool_calls=[edit("file.txt", old, value)]),
                LLMResponse(content="private-fixer-response-do-not-copy"),
            ]
        )
        old = value
    return SequenceLLM(responses)


def reviewer(owned: WorkflowGit, llm: Any, **kwargs: Any) -> FreshReviewer:
    return FreshReviewer(
        owned, llm, budget=RunBudget(max_steps=2), timeout_seconds=5, **kwargs
    )


@pytest.mark.parametrize("warnings", [False, True])
def test_complete_review_without_blockers_passes_without_running_fixer(
    provider: WorktreeProvider, warnings: bool
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        llm = TreeReviewLLM([[finding("warning")] if warnings else []])
        fix_llm = SequenceLLM([])
        verifier = RecordingVerifier()
        before = filesystem_state(owned.handle.path)
        index = filesystem_state(owned.handle._admin_path)
        result = await ReviewWorkflow(
            owned,
            reviewer(owned, llm),
            verifier,
            max_fix_iterations=3,
            fixer_llm=fix_llm,
        ).run(TASK, snapshot, verified)
        assert result.status is ReviewWorkflowStatus.REVIEW_PASSED
        assert result.failure is None
        assert result.snapshot is snapshot and result.verification is verified
        assert len(result.rounds) == 1 and not result.fix_results
        assert len(llm.calls) == 1 and not fix_llm.calls
        assert not verifier.snapshots
        assert result.rounds[0].review.status is ReviewStatus.COMPLETED
        assert all(
            item.severity is ReviewSeverity.WARNING
            for item in result.rounds[0].review.findings
        )
        assert result.handle is owned.handle
        assert result.handle.state is WorktreeState.RETAINED
        assert result.recovery_path.exists()
        assert not result.recovery_path.is_relative_to(result.handle.path)
        assert filesystem_state(owned.handle.path) == before
        assert filesystem_state(owned.handle._admin_path) == index
        with pytest.raises(FrozenInstanceError):
            result.status = ReviewWorkflowStatus.CANCELLED  # type: ignore[misc]

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["fixer", "verifier"])
def test_blocker_requires_fix_capability_and_reverification_before_any_fix(
    provider: WorktreeProvider, missing: str
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        llm = TreeReviewLLM([[finding()]])
        fix_llm = SequenceLLM([])
        result = await ReviewWorkflow(
            owned,
            reviewer(owned, llm),
            None if missing == "verifier" else RecordingVerifier(),
            max_fix_iterations=1,
            fixer_llm=None if missing == "fixer" else fix_llm,
        ).run(TASK, snapshot, verified)
        assert result.status is (
            ReviewWorkflowStatus.FIX_ERROR
            if missing == "fixer"
            else ReviewWorkflowStatus.VERIFICATION_FAILED
        )
        assert result.failure is (
            ReviewWorkflowFailure.MISSING_FIXER
            if missing == "fixer"
            else ReviewWorkflowFailure.MISSING_VERIFIER
        )
        assert len(llm.calls) == len(result.rounds) == 1
        assert not fix_llm.calls and not result.fix_results
        assert result.verification is verified

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["missing-trace", "unchanged-path"])
def test_invalid_completed_adapter_result_cannot_approve_or_start_fixer(
    provider: WorktreeProvider, kind: str
) -> None:
    class InvalidReviewer(FreshReviewer):
        async def review(
            self,
            task: TaskSpec,
            snapshot: GitSnapshot,
            verification: VerificationResult | None,
        ) -> ReviewResult:
            findings = []
            if kind == "unchanged-path":
                assert ".gitignore" not in snapshot.changed_files
                findings = [finding() | {"path": ".gitignore"}]
            return ReviewResult.model_validate_json(
                json.dumps(
                    {
                        "status": "completed",
                        "reviewed_tree": snapshot.tree_revision,
                        "trace_id": None if kind == "missing-trace" else "1" * 32,
                        "findings": findings,
                    }
                )
            )

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        review_llm = SequenceLLM([])
        fix_llm = SequenceLLM([])
        verifier = RecordingVerifier()
        result = await ReviewWorkflow(
            owned,
            InvalidReviewer(
                owned, review_llm, budget=RunBudget(max_steps=1), timeout_seconds=5
            ),
            verifier,
            max_fix_iterations=1,
            fixer_llm=fix_llm,
        ).run(TASK, snapshot, verified)
        assert result.status is ReviewWorkflowStatus.REVIEW_ERROR
        assert result.failure is ReviewWorkflowFailure.REVIEW_FAILED
        assert not fix_llm.calls and not result.fix_results
        assert not verifier.snapshots and not review_llm.calls
        assert result.recovery_path.exists()

    asyncio.run(scenario())


def test_blocker_runs_fresh_fixer_then_verifies_new_tree_and_reviews_from_clean_history(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    runners: list[CodingTaskRunner] = []
    specs: list[TaskSpec] = []

    class RecordingRunner(CodingTaskRunner):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            runners.append(self)

        async def run(
            self, spec: TaskSpec, *, cancellation_event: asyncio.Event | None = None
        ) -> Any:
            specs.append(spec)
            return await super().run(spec, cancellation_event=cancellation_event)

    monkeypatch.setattr(workflow_module, "CodingTaskRunner", RecordingRunner)

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        blocker = finding()
        blocker["id"] = "private-finding-id-do-not-persist"
        blocker["evidence"] = "private-blocker-evidence-do-not-persist"
        warning = finding("warning") | {"id": "W1", "summary": "private-warning-only"}
        llm = TreeReviewLLM([[blocker, warning], []])
        fix_llm = fixer("fixed\n")
        verifier = RecordingVerifier()
        sink = RecordingSink()
        trace = Tracer(sink)
        events: list[Event] = []

        def record_event(event: Event) -> None:
            events.append(event)

        result = await ReviewWorkflow(
            owned,
            reviewer(owned, llm, tracer=trace, event_handler=record_event),
            verifier,
            max_fix_iterations=1,
            fixer_llm=fix_llm,
            tracer=trace,
            event_handler=record_event,
        ).run(TASK, snapshot, verified)
        assert result.status is ReviewWorkflowStatus.REVIEW_PASSED
        assert result.snapshot is not None and result.snapshot != snapshot
        assert result.snapshot.tree_revision == git(result.handle.path, "write-tree")
        assert verifier.snapshots == [result.snapshot]
        assert verifier.contents == ["fixed\n"]
        assert result.verification is not None and result.verification.passed
        assert result.verification.tree_revision == result.snapshot.tree_revision
        assert len(result.rounds) == 2 and len(result.fix_results) == 1
        assert len(runners) == 1 and len(specs) == 1
        assert runners[0].workspace.root == owned.handle.workspace.root
        assert TASK.prompt in specs[0].prompt
        assert blocker["evidence"] in specs[0].prompt
        assert warning["summary"] not in specs[0].prompt
        assert result.fix_results[0].status is TaskStatus.COMPLETED
        assert len(fix_llm.calls) == 2
        assert len(llm.calls) == 2
        for messages in llm.calls:
            assert [message.role for message in messages] == ["system", "user"]
            assert "private-fixer-response" not in json.dumps(
                [message.model_dump() for message in messages]
            )
        traces = [
            result.rounds[0].review.trace_id,
            result.fix_results[0].trace_id,
            result.rounds[1].review.trace_id,
        ]
        assert all(trace_id is not None for trace_id in traces)
        assert len(set(traces)) == 3
        assert [
            event.data["trace_id"] for event in events if event.type == "trace_start"
        ] == traces
        assert len([span for span in sink.spans if span.name == "agent.turn"]) == 3
        stored = result.recovery_path.read_text()
        assert "private-finding" not in stored
        assert "private-blocker" not in stored
        assert "private-warning" not in stored
        assert "private-fixer" not in stored
        assert TASK.prompt not in stored
        assert "diff --git" not in stored
        report = json.loads(stored)
        assert report["status"] == "review_passed"
        assert len(report["rounds"]) == 2 and len(report["fixes"]) == 1
        assert report["fixes"][0]["trace_id"] == traces[1]
        assert result.recovery_path.stat().st_mode & 0o077 == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, 2])
def test_persistent_blockers_stop_at_limit_and_use_fresh_fix_runners(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    runners: list[CodingTaskRunner] = []

    class RecordingRunner(CodingTaskRunner):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            runners.append(self)

    monkeypatch.setattr(workflow_module, "CodingTaskRunner", RecordingRunner)

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        llm = TreeReviewLLM([[finding()] for _ in range(limit + 1)])
        fix_llm = fixer("first\n", "second\n")
        verifier = RecordingVerifier()
        result = await ReviewWorkflow(
            owned,
            reviewer(owned, llm),
            verifier,
            max_fix_iterations=limit,
            fixer_llm=fix_llm,
        ).run(TASK, snapshot, verified)
        assert result.status is ReviewWorkflowStatus.NEEDS_HUMAN_REVIEW
        assert result.failure is ReviewWorkflowFailure.ITERATION_LIMIT
        assert len(result.rounds) == limit + 1
        assert len(result.fix_results) == len(runners) == limit
        assert len(verifier.snapshots) == limit and len(fix_llm.calls) == limit * 2
        assert len({id(runner.context_builder) for runner in runners}) == limit
        assert len({item.review.trace_id for item in result.rounds}) == limit + 1
        assert all(
            item.review.status is ReviewStatus.COMPLETED for item in result.rounds
        )
        assert result.handle.state is WorktreeState.RETAINED

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["missing", "failed", "stale", "malformed"])
def test_invalid_initial_verification_never_starts_review_or_fix(
    provider: WorktreeProvider, kind: str
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        verification: Any = verified
        if kind == "missing":
            verification = None
        elif kind == "failed":
            verification = VerificationResult(
                snapshot.tree_revision, (VerificationCheck("fixed", False),)
            )
        elif kind == "stale":
            verification = replace(verified, tree_revision="0" * 40)
        else:
            verification = {"tree_revision": snapshot.tree_revision, "passed": True}
        llm = TreeReviewLLM([])
        fix_llm = SequenceLLM([])
        verifier = RecordingVerifier()
        result = await ReviewWorkflow(
            owned,
            reviewer(owned, llm),
            verifier,
            max_fix_iterations=1,
            fixer_llm=fix_llm,
        ).run(TASK, snapshot, verification)
        assert result.status is ReviewWorkflowStatus.VERIFICATION_FAILED
        assert result.failure is ReviewWorkflowFailure.INVALID_VERIFICATION
        assert not result.rounds and not result.fix_results
        assert not llm.calls and not fix_llm.calls and not verifier.snapshots
        assert result.verification is None
        assert result.handle.state is WorktreeState.RETAINED

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind",
    ["runtime", "json", "duplicate-id", "drift", "incomplete", "budget", "timeout"],
)
def test_review_failures_never_start_a_fixer(
    provider: WorktreeProvider, kind: str
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        llm: Any = TreeReviewLLM(["not JSON"])
        expected = ReviewStatus.MALFORMED_OUTPUT
        if kind == "runtime":
            llm, expected = FailingLLM(), ReviewStatus.RUNTIME_ERROR
        elif kind == "duplicate-id":
            llm = TreeReviewLLM([[finding(), finding("warning")]])
        elif kind == "incomplete":
            llm = TreeReviewLLM(
                [
                    json.dumps(
                        {
                            "reviewed_tree": snapshot.tree_revision,
                            "complete": False,
                            "findings": [],
                        }
                    )
                ]
            )
            expected = ReviewStatus.INCOMPLETE
        elif kind == "drift":

            class MutatingLLM(TreeReviewLLM):
                async def generate(
                    self,
                    messages: list[Message],
                    tools: list[dict[str, Any]] | None = None,
                ) -> LLMResponse:
                    (owned.handle.path / "file.txt").write_text("unverified\n")
                    return await super().generate(messages, tools)

            llm = MutatingLLM([[]])
            expected = ReviewStatus.SNAPSHOT_DRIFT
        elif kind == "budget":
            llm = SequenceLLM(
                [
                    LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="read",
                                name="read_file",
                                arguments={"path": "file.txt"},
                            )
                        ]
                    )
                ]
            )
            expected = ReviewStatus.BUDGET_EXHAUSTED
        elif kind == "timeout":

            class BlockingLLM:
                async def generate(
                    self,
                    messages: list[Message],
                    tools: list[dict[str, Any]] | None = None,
                ) -> LLMResponse:
                    await asyncio.Event().wait()
                    raise AssertionError("unreachable")

            llm = BlockingLLM()
            expected = ReviewStatus.TIMED_OUT
        fix_llm = SequenceLLM([])
        result = await ReviewWorkflow(
            owned,
            FreshReviewer(
                owned,
                llm,
                budget=RunBudget(max_steps=1 if kind == "budget" else 2),
                timeout_seconds=0.01 if kind == "timeout" else 5,
            ),
            RecordingVerifier(),
            max_fix_iterations=1,
            fixer_llm=fix_llm,
        ).run(TASK, snapshot, verified)
        assert result.status is (
            ReviewWorkflowStatus.REVIEW_INCOMPLETE
            if kind == "incomplete"
            else ReviewWorkflowStatus.REVIEW_ERROR
        )
        assert len(result.rounds) == 1 and result.rounds[0].review.status is expected
        assert not result.fix_results and not fix_llm.calls
        assert result.recovery_path.exists()
        if kind == "drift":
            assert result.verification is None
            assert result.rounds[0].verification is verified
            assert (
                json.loads(result.recovery_path.read_text())["verification_checks"]
                == []
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["runtime", "budget", "timeout", "no-progress", "revert"]
)
def test_fix_failures_clear_verification_and_do_not_review_again(
    provider: WorktreeProvider, kind: str
) -> None:
    class BlockingLLM:
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        review_llm = TreeReviewLLM([[finding()]])
        fix_llm: Any = {
            "runtime": FailingLLM(),
            "budget": fixer("fixed\n"),
            "timeout": BlockingLLM(),
            "no-progress": SequenceLLM([LLMResponse(content="No repair made")]),
            "revert": fixer("before\n"),
        }[kind]
        verifier = RecordingVerifier()
        result = await ReviewWorkflow(
            owned,
            reviewer(owned, review_llm),
            verifier,
            max_fix_iterations=1,
            fixer_llm=fix_llm,
            fix_budget=RunBudget(max_steps=1 if kind == "budget" else 2),
            fix_timeout_seconds=0.01 if kind == "timeout" else 5,
        ).run(TASK, snapshot, verified)
        expected_failure = {
            "runtime": ReviewWorkflowFailure.FIX_RUNTIME_ERROR,
            "budget": ReviewWorkflowFailure.FIX_BUDGET_EXHAUSTED,
            "timeout": ReviewWorkflowFailure.FIX_TIMEOUT,
            "no-progress": ReviewWorkflowFailure.NO_PROGRESS,
            "revert": ReviewWorkflowFailure.NO_CHANGES,
        }[kind]
        assert result.failure is expected_failure
        assert (
            result.status
            is {
                "runtime": ReviewWorkflowStatus.FIX_ERROR,
                "budget": ReviewWorkflowStatus.FIX_ERROR,
                "timeout": ReviewWorkflowStatus.FIX_ERROR,
                "no-progress": ReviewWorkflowStatus.NEEDS_HUMAN_REVIEW,
                "revert": ReviewWorkflowStatus.VERIFICATION_FAILED,
            }[kind]
        )
        assert len(result.rounds) == 1 and len(review_llm.calls) == 1
        assert result.verification is None
        assert not verifier.snapshots
        if kind != "timeout":
            assert len(result.fix_results) == 1
        assert result.handle.state is WorktreeState.RETAINED
        assert result.recovery_path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["failed", "stale", "malformed", "runtime", "timeout", "tracked", "ignored"]
)
def test_reverification_is_required_and_mutating_verifiers_cannot_approve(
    provider: WorktreeProvider, kind: str
) -> None:
    class Verifier:
        async def verify(self, workspace: Workspace, snapshot: GitSnapshot) -> Any:
            if kind == "runtime":
                raise RuntimeError("private-verifier-error-do-not-persist")
            if kind == "timeout":
                await asyncio.Event().wait()
            if kind == "malformed":
                return {"tree_revision": snapshot.tree_revision, "passed": True}
            if kind == "tracked":
                (workspace.root / "file.txt").write_text("mutation\n")
            elif kind == "ignored":
                (workspace.root / "ignored").write_text("mutation\n")
            return VerificationResult(
                "0" * 40 if kind == "stale" else snapshot.tree_revision,
                (VerificationCheck("fixed", kind != "failed"),),
            )

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        llm = TreeReviewLLM([[finding()]])
        result = await ReviewWorkflow(
            owned,
            reviewer(owned, llm),
            Verifier(),
            max_fix_iterations=1,
            fixer_llm=fixer("fixed\n"),
            verification_timeout_seconds=0.01 if kind == "timeout" else 5,
        ).run(TASK, snapshot, verified)
        assert result.status is ReviewWorkflowStatus.VERIFICATION_FAILED
        assert (
            result.failure
            is {
                "failed": ReviewWorkflowFailure.VERIFICATION_FAILED,
                "stale": ReviewWorkflowFailure.INVALID_VERIFICATION,
                "malformed": ReviewWorkflowFailure.INVALID_VERIFICATION,
                "runtime": ReviewWorkflowFailure.VERIFICATION_ERROR,
                "timeout": ReviewWorkflowFailure.VERIFICATION_TIMEOUT,
                "tracked": ReviewWorkflowFailure.SNAPSHOT_DRIFT,
                "ignored": ReviewWorkflowFailure.SNAPSHOT_DRIFT,
            }[kind]
        )
        assert len(result.rounds) == len(result.fix_results) == len(llm.calls) == 1
        assert "private-verifier-error" not in result.recovery_path.read_text()
        if kind == "failed":
            assert result.snapshot is not None and result.snapshot != snapshot
            assert result.verification is not None and not result.verification.passed
            assert result.verification.tree_revision == result.snapshot.tree_revision
            assert result.verification.checks == (VerificationCheck("fixed", False),)
            report = json.loads(result.recovery_path.read_text())
            assert report["tree_revision"] == result.snapshot.tree_revision
            assert report["verification_checks"] == [
                {"check_id": "fixed", "passed": False, "category": None}
            ]
            assert report["rounds"][0]["checks"] == [
                {"check_id": "fixed", "passed": True, "category": None}
            ]
        else:
            assert result.verification is None

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["fixer", "staging", "verifier", "reviewer"])
@pytest.mark.parametrize("external", [False, True])
def test_cancel_settles_active_phase_and_retains_completed_round_and_fix_evidence(
    provider: WorktreeProvider,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    external: bool,
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        entered, cleaning, resume = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancellation = asyncio.Event()

        async def block() -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                if external:
                    await resume.wait()
                raise

        original_stage = owned.stage

        async def stage() -> GitSnapshot:
            await block()
            return await original_stage()

        if phase == "staging":
            monkeypatch.setattr(owned, "stage", stage)

        class ReviewLLM(TreeReviewLLM):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                if phase == "reviewer" and len(self.calls) == 1:
                    await block()
                return await super().generate(messages, tools)

        class FixLLM(SequenceLLM):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                if phase == "fixer" and len(self.calls) == 2:
                    await block()
                return await super().generate(messages, tools)

        class Verifier(RecordingVerifier):
            async def verify(
                self, workspace: Workspace, snapshot: GitSnapshot
            ) -> VerificationResult:
                if phase == "verifier":
                    await block()
                return await super().verify(workspace, snapshot)

        review_llm = ReviewLLM([[finding()], [finding()]])
        fix_llm = FixLLM(fixer("first\n", "second\n").responses)
        running = asyncio.create_task(
            ReviewWorkflow(
                owned,
                reviewer(owned, review_llm),
                Verifier(),
                max_fix_iterations=2,
                fixer_llm=fix_llm,
                fix_timeout_seconds=10,
                verification_timeout_seconds=10,
            ).run(TASK, snapshot, verified, cancellation_event=cancellation)
        )
        await asyncio.wait_for(entered.wait(), 5)
        recovery = owned.handle.path.parent / (owned.handle.path.name + ".review.json")
        if external:
            running.cancel("first external cancellation")
            await asyncio.wait_for(cleaning.wait(), 5)
            running.cancel("second external cancellation")
            await asyncio.sleep(0)
            assert not running.done()
            resume.set()
            with pytest.raises(
                asyncio.CancelledError, match="first external cancellation"
            ) as error:
                await asyncio.wait_for(running, 5)
            assert any(str(recovery) in note for note in error.value.__notes__)
        else:
            cancellation.set()
            result = await asyncio.wait_for(running, 5)
            assert result.status is ReviewWorkflowStatus.CANCELLED
            assert result.rounds[0].review.status is ReviewStatus.COMPLETED
            assert result.fix_results[0].status is TaskStatus.COMPLETED
            assert len(result.rounds) >= 1 and len(result.fix_results) >= 1
            if phase != "reviewer":
                assert result.verification is None
        assert cleaning.is_set()
        assert (
            owned.handle.state is WorktreeState.RETAINED and owned.handle.path.exists()
        )
        assert recovery.exists()
        report = json.loads(recovery.read_text())
        assert report["status"] == "cancelled"
        assert report["rounds"][0]["review_status"] == "completed"
        assert report["fixes"][0]["task"]["status"] == "completed"
        assert "private-fixer-response" not in recovery.read_text()
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    asyncio.run(scenario())


def test_pre_cancelled_workflow_never_starts_a_model(
    provider: WorktreeProvider,
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        cancellation = asyncio.Event()
        cancellation.set()
        llm = TreeReviewLLM([])
        result = await ReviewWorkflow(
            owned, reviewer(owned, llm), RecordingVerifier(), max_fix_iterations=0
        ).run(TASK, snapshot, verified, cancellation_event=cancellation)
        assert result.status is ReviewWorkflowStatus.CANCELLED
        assert not llm.calls and not result.rounds and not result.fix_results
        assert result.recovery_path.exists()

    asyncio.run(scenario())


def test_provider_declared_secrets_are_inherited_by_real_fixer_children(
    provider: WorktreeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_working_sandbox(provider.source)
    secrets = {
        "CUSTOM_SERVICE_TOKEN": "fake-custom-service-credential",
        # Git permits LANG normally: this catches loss of the declared names
        # in both live Agent repository context and final evidence inspection.
        "LANG": "fake-custom-language-credential",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("REVIEW_CHILD_CONTROL", "ordinary-variable-visible")
    log = install_git_recorder(tmp_path, monkeypatch)
    declared_provider = WorktreeProvider(
        provider.source, provider.parent, secret_env_keys=frozenset(secrets)
    )
    boundaries: dict[str, int] = {}
    model_boundaries: list[int] = []
    tool_results: list[dict[str, Any]] = []

    def record_event(event: Event) -> None:
        if event.type in {"trace_start", "trace_finish"}:
            boundaries[event.type] = len(records(log))
        elif event.type == "tool_result" and event.data["tool"] == "bash":
            tool_results.append(event.data)

    class FixerLLM(SequenceLLM):
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            assert any(
                message.role == "system"
                and "Runtime repository context:" in (message.content or "")
                for message in messages
            )
            model_boundaries.append(len(records(log)))
            return await super().generate(messages, tools)

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(declared_provider)
        fix_llm = FixerLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="environment-probe",
                            name="bash",
                            arguments={
                                "command": 'printf "%s\\n" "${CUSTOM_SERVICE_TOKEN+present}" "${LANG+present}" "$REVIEW_CHILD_CONTROL"'
                            },
                        )
                    ]
                ),
                *fixer("fixed\n").responses,
            ]
        )
        result = await ReviewWorkflow(
            owned,
            reviewer(owned, TreeReviewLLM([[finding()], []])),
            RecordingVerifier(),
            max_fix_iterations=1,
            fixer_llm=fix_llm,
            event_handler=record_event,
        ).run(TASK, snapshot, verified)
        assert result.status is ReviewWorkflowStatus.REVIEW_PASSED
        assert len(result.fix_results) == 1
        assert result.fix_results[0].repository.is_git_repository is True
        assert result.fix_results[0].repository.inspection_error is None
        assert len(tool_results) == 1
        assert tool_results[0]["exit_code"] == 0
        assert tool_results[0]["stdout"] == "\n\nordinary-variable-visible\n"
        observed = records(log)
        prompt_children = observed[boundaries["trace_start"] : model_boundaries[0]]
        assert len(prompt_children) == 3
        assert prompt_children[0]["args"][-2:] == ["rev-parse", "--show-toplevel"]
        assert "--untracked-files=normal" in prompt_children[-1]["args"]
        final_children = observed[boundaries["trace_finish"] :][:4]
        assert len(final_children) == 4
        assert final_children[0]["args"][-2:] == ["rev-parse", "--show-toplevel"]
        assert final_children[2]["args"][-4:] == [
            "rev-parse",
            "--verify",
            "--quiet",
            "HEAD^{commit}",
        ]
        assert "--untracked-files=all" in final_children[-1]["args"]
        assert "-z" in final_children[-1]["args"]
        for child in observed:
            assert not (secrets.keys() & child["env"].keys())
            assert child["env"]["HOME"] == os.environ["HOME"]
            assert child["env"]["PATH"] == os.environ["PATH"]
        assert {name: os.environ[name] for name in secrets} == secrets
        assert os.environ["REVIEW_CHILD_CONTROL"] == "ordinary-variable-visible"
        for value in secrets.values():
            assert value not in log.read_text()
            assert value not in result.recovery_path.read_text()

    asyncio.run(scenario())
