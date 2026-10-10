import asyncio
import json
from typing import Any

import pytest

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, Message
from cairn.evals.checks import FileExistsCheck
from cairn.git import WorktreeProvider, WorktreeState
from cairn.review import FreshReviewer
from cairn.review import workflow as workflow_module
from cairn.review.workflow import ReviewWorkflow
from cairn.review.workflow_models import ReviewPersistenceError, ReviewWorkflowStatus
from cairn.workflow.verification import FixedChecksVerifier
from tests.review.test_reviewer import TASK, finding, proposal
from tests.support.runtime import SequenceLLM
from tests.workflow.test_local import edit
from tests.workflow.test_local import provider as provider


def test_one_fix_runs_real_task_and_verifier_before_a_fresh_review(
    provider: WorktreeProvider,
) -> None:
    class ReviewLLM:
        calls = 0

        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            self.calls += 1
            assert len(messages) == 2
            prompt = json.loads(messages[-1].content or "")
            return LLMResponse(
                content=json.dumps(
                    {
                        "reviewed_tree": prompt["trusted_repository_facts"][
                            "tree_revision"
                        ],
                        "complete": True,
                        "findings": [finding()] if self.calls == 1 else [],
                    }
                )
            )

    async def scenario() -> None:
        owned, initial, verified = await proposal(provider)
        review_llm = ReviewLLM()
        result = await ReviewWorkflow(
            owned,
            FreshReviewer(
                owned, review_llm, budget=RunBudget(max_steps=2), timeout_seconds=5
            ),
            FixedChecksVerifier([FileExistsCheck("file.txt")]),
            max_fix_iterations=1,
            fixer_llm=SequenceLLM(
                [
                    LLMResponse(tool_calls=[edit("file.txt", "after\n", "repaired\n")]),
                    LLMResponse(content="Repair completed"),
                ]
            ),
            fix_budget=RunBudget(max_steps=2),
        ).run(TASK, initial, verified)
        assert result.status is ReviewWorkflowStatus.REVIEW_PASSED
        assert result.snapshot is not None and result.verification is not None
        assert result.snapshot.tree_revision != initial.tree_revision
        assert result.verification.tree_revision == result.snapshot.tree_revision
        assert result.verification.passed
        assert len(result.rounds) == 2 and len(result.fix_results) == 1
        assert (
            len(
                {round.review.trace_id for round in result.rounds}
                | {result.fix_results[0].trace_id}
            )
            == 3
        )
        assert result.handle.state is WorktreeState.RETAINED
        report = json.loads(result.recovery_path.read_text())
        assert report["tree_revision"] == result.snapshot.tree_revision
        assert report["verification_checks"] == [
            {"check_id": "1:FileExistsCheck", "passed": True, "category": None}
        ]
        assert "Repair completed" not in result.recovery_path.read_text()

    asyncio.run(scenario())


def test_recovery_write_failure_is_observable_and_keeps_the_worktree(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("private-report-error-with-fake-secret")

    monkeypatch.setattr(workflow_module, "persist_review_report", fail)

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        llm = SequenceLLM([])
        with pytest.raises(ReviewPersistenceError) as error:
            await ReviewWorkflow(
                owned,
                FreshReviewer(
                    owned, llm, budget=RunBudget(max_steps=1), timeout_seconds=5
                ),
                None,
                max_fix_iterations=0,
            ).run(TASK, snapshot, verified)
        assert owned.handle.state is WorktreeState.RETAINED
        assert (owned.handle.path / "file.txt").read_text() == "after\n"
        assert not llm.calls
        assert any(str(owned.handle.path) in note for note in error.value.__notes__)
        assert "fake-secret" not in str(error.value)
        assert "fake-secret" not in " ".join(error.value.__notes__)
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    asyncio.run(scenario())


def test_external_cancel_during_cooperative_cleanup_still_propagates(
    provider: WorktreeProvider,
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        entered, cleaning, resume = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancellation = asyncio.Event()

        class BlockingLLM:
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleaning.set()
                    await resume.wait()
                    raise
                raise AssertionError("unreachable")

        workflow = ReviewWorkflow(
            owned,
            FreshReviewer(
                owned, BlockingLLM(), budget=RunBudget(max_steps=2), timeout_seconds=5
            ),
            None,
            max_fix_iterations=0,
        )
        task = asyncio.create_task(
            workflow.run(TASK, snapshot, verified, cancellation_event=cancellation)
        )
        await asyncio.wait_for(entered.wait(), 5)
        cancellation.set()
        await asyncio.wait_for(cleaning.wait(), 5)
        task.cancel("first external cancellation")
        await asyncio.sleep(0)
        task.cancel("repeated external cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        resume.set()
        with pytest.raises(asyncio.CancelledError) as failure:
            await task
        assert failure.value.args == ("first external cancellation",)
        assert owned.handle.state is WorktreeState.RETAINED
        path = owned.handle.path.parent / f"{owned.handle.path.name}.review.json"
        report = json.loads(path.read_text())
        assert report["status"] == "cancelled"
        assert report["verification_checks"] == []
        assert not [
            active
            for active in asyncio.all_tasks()
            if active is not asyncio.current_task()
        ]

    asyncio.run(scenario())
