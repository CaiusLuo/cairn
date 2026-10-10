import asyncio
import json
from typing import Any

import pytest

from cairn.core.agent import Agent
from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.git import WorktreeProvider
from cairn.review import (
    FreshReviewer,
    ReviewSeverity,
    ReviewStatus,
)
from cairn.review import reviewer as reviewer_module
from cairn.tasks import TaskSpec
from cairn.workflow import (
    GitSnapshot,
    VerificationCheck,
    VerificationResult,
    WorkflowGit,
)
from tests.support.runtime import SequenceLLM
from tests.workflow.test_local import git
from tests.workflow.test_local import provider as provider

TASK = TaskSpec(task_id="review-task", prompt="Update file.txt to say after")


async def proposal(
    provider: WorktreeProvider,
) -> tuple[WorkflowGit, GitSnapshot, VerificationResult]:
    handle = await provider.create("HEAD", "codex/fresh-review")
    handle.retain()
    (handle.path / "file.txt").write_text("after\n")
    owned = WorkflowGit(handle)
    snapshot = await owned.stage()
    return (
        owned,
        snapshot,
        VerificationResult(snapshot.tree_revision, (VerificationCheck("fixed", True),)),
    )


def output(snapshot: GitSnapshot, findings: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "reviewed_tree": snapshot.tree_revision,
            "complete": True,
            "findings": findings,
        }
    )


def finding(severity: str = "blocker") -> dict[str, Any]:
    return {
        "id": "F1",
        "severity": severity,
        "summary": "The new value breaks consumers expecting before",
        "path": "file.txt",
        "line": 1,
        "evidence": "Line 1 now contains after, which changes the consumed value",
        "suggested_direction": "Preserve the expected consumed value",
    }


@pytest.mark.parametrize("severity", [None, "blocker", "warning"])
def test_review_accepts_complete_strict_findings_and_actual_trace(
    provider: WorktreeProvider, severity: str | None
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        findings = [] if severity is None else [finding(severity)]
        llm = SequenceLLM([LLMResponse(content=output(snapshot, findings))])
        result = await FreshReviewer(
            owned, llm, budget=RunBudget(max_steps=2), timeout_seconds=5
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.COMPLETED
        assert result.reviewed_tree == snapshot.tree_revision
        assert result.trace_id is not None and len(result.trace_id) == 32
        assert len(result.findings) == len(findings)
        if severity is not None:
            assert result.findings[0].severity is ReviewSeverity(severity)
        prompt = json.loads(llm.calls[0][0][-1].content or "")
        assert prompt["untrusted_original_task"] == TASK.prompt
        assert "-before\n+after" in prompt["untrusted_exact_diff"]
        assert prompt["trusted_repository_facts"]["verification_checks"] == [
            {"check_id": "fixed", "passed": True}
        ]
        assert (owned.handle.path / "file.txt").read_text() == "after\n"
        await owned.assert_unchanged(snapshot)

    asyncio.run(scenario())


@pytest.mark.parametrize("detect_renames", [True, False])
def test_rename_uses_snapshot_paths_and_supplies_complete_deletion_addition_diff(
    provider: WorktreeProvider, detect_renames: bool
) -> None:
    if not detect_renames:
        git(provider.source.root, "config", "diff.renames", "false")

    async def scenario() -> None:
        handle = await provider.create("HEAD", "codex/review-rename")
        handle.retain()
        (handle.path / "file.txt").rename(handle.path / "renamed.txt")
        owned = WorkflowGit(handle)
        snapshot = await owned.stage()
        assert snapshot.changed_files == (
            ("renamed.txt",) if detect_renames else ("file.txt", "renamed.txt")
        )
        verified = VerificationResult(
            snapshot.tree_revision, (VerificationCheck("rename", True),)
        )
        llm = SequenceLLM([LLMResponse(content=output(snapshot, []))])
        result = await FreshReviewer(
            owned, llm, budget=RunBudget(max_steps=1), timeout_seconds=5
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.COMPLETED
        prompt = json.loads(llm.calls[0][0][-1].content or "")
        diff = prompt["untrusted_exact_diff"]
        assert "diff --git a/file.txt b/file.txt" in diff
        assert "diff --git a/renamed.txt b/renamed.txt" in diff
        assert "-before\n" in diff and "+before\n" in diff
        assert (handle.path / "renamed.txt").read_text() == "before\n"

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["text", "duplicate-key", "duplicate-id", "evidence"])
def test_review_rejects_malformed_or_unsubstantiated_output(
    provider: WorktreeProvider, kind: str
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        if kind == "text":
            response = "I approve this change"
        elif kind == "duplicate-key":
            response = output(snapshot, []).replace(
                '"complete": true', '"complete": false, "complete": true'
            )
        elif kind == "duplicate-id":
            response = output(snapshot, [finding(), finding("warning")])
        else:
            item = finding()
            del item["evidence"]
            response = output(snapshot, [item])
        result = await FreshReviewer(
            owned,
            SequenceLLM([LLMResponse(content=response)]),
            budget=RunBudget(max_steps=2),
            timeout_seconds=5,
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.MALFORMED_OUTPUT
        assert not result.findings
        assert result.trace_id is not None
        assert "I approve" not in result.model_dump_json()
        assert set(result.model_dump()) == {
            "status",
            "reviewed_tree",
            "trace_id",
            "findings",
        }

    asyncio.run(scenario())


def test_every_review_has_fresh_agent_state_context_and_read_only_tools(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents: list[Agent] = []

    class InspectingAgent(Agent):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            agents.append(self)
            assert self.state.messages == []
            assert set(self.tools.tools) == {"read_file"}
            assert self.permission_handler is None
            assert self.repo_context_provider is None

    class ReadThenFinish:
        def __init__(self, response: str) -> None:
            self.response = response
            self.calls = 0

        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            assert tools is not None
            assert [item["function"]["name"] for item in tools] == ["read_file"]
            self.calls += 1
            if self.calls % 2:
                assert len(messages) == 2
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="read", name="read_file", arguments={"path": "file.txt"}
                        )
                    ]
                )
            tool_result = json.loads(messages[-1].content or "")
            assert tool_result["stdout"] == "after\n"
            return LLMResponse(content=self.response)

    monkeypatch.setattr(reviewer_module, "Agent", InspectingAgent)

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        reviewer = FreshReviewer(
            owned,
            ReadThenFinish(output(snapshot, [])),
            budget=RunBudget(max_steps=2),
            timeout_seconds=5,
        )
        first = await reviewer.review(TASK, snapshot, verified)
        second = await reviewer.review(TASK, snapshot, verified)
        assert first.status is second.status is ReviewStatus.COMPLETED
        assert first.trace_id != second.trace_id
        assert len(agents) == 2 and agents[0] is not agents[1]
        assert agents[0].state is not agents[1].state
        assert agents[0].context_builder is not agents[1].context_builder
        assert len(agents[0].state.messages) == len(agents[1].state.messages) == 4
        assert (owned.handle.path / "file.txt").read_text() == "after\n"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("exact-bytes", ReviewStatus.COMPLETED),
        ("ascii-overflow", ReviewStatus.MALFORMED_OUTPUT),
        ("utf8-overflow", ReviewStatus.MALFORMED_OUTPUT),
        ("exact-findings", ReviewStatus.COMPLETED),
        ("too-many-findings", ReviewStatus.MALFORMED_OUTPUT),
    ],
)
def test_review_bounds_output_bytes_and_finding_count(
    provider: WorktreeProvider, kind: str, expected: ReviewStatus
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        limit = reviewer_module.MAX_REVIEW_OUTPUT_BYTES
        if kind == "utf8-overflow":
            item = finding()
            item["evidence"] = "证据" * 200
            response = json.dumps(
                {
                    "reviewed_tree": snapshot.tree_revision,
                    "complete": True,
                    "findings": [item],
                },
                ensure_ascii=False,
            )
            response += " " * (limit + 1 - len(response.encode("utf-8")))
            assert len(response) < limit < len(response.encode("utf-8"))
        elif kind in ("exact-findings", "too-many-findings"):
            count = 50 if kind == "exact-findings" else 51
            findings = [
                dict(finding(), id=f"F{index}", line=index)
                for index in range(1, count + 1)
            ]
            response = output(snapshot, findings)
            assert len(response.encode("utf-8")) < limit
        else:
            response = output(snapshot, [])
            size = limit if kind == "exact-bytes" else limit + 1
            response += " " * (size - len(response.encode("utf-8")))
        result = await FreshReviewer(
            owned,
            SequenceLLM([LLMResponse(content=response)]),
            budget=RunBudget(max_steps=2),
            timeout_seconds=5,
        ).review(TASK, snapshot, verified)
        assert result.status is expected
        assert result.trace_id is not None
        assert len(result.findings) == (50 if kind == "exact-findings" else 0)
        assert (owned.handle.path / "file.txt").read_text() == "after\n"
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    asyncio.run(scenario())
