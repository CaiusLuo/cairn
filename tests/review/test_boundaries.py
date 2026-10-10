import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from cairn.core.budget import RunBudget
from cairn.core.events import Event
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.git import WorktreeProvider, WorktreeState
from cairn.review import FreshReviewer, ReviewStatus
from cairn.review import git as review_git_module
from cairn.review.git import MAX_DIFF_BYTES
from cairn.workflow import (
    GitSnapshot,
    VerificationCheck,
    VerificationResult,
    WorkflowGit,
)
from tests.review.test_reviewer import TASK, finding, output, proposal
from tests.support.runtime import SequenceLLM
from tests.workflow.test_local import git
from tests.workflow.test_local import provider as provider


def filesystem_state(root: Path) -> dict[str, tuple[Any, ...]]:
    paths = [root, *sorted(root.rglob("*"))]
    result: dict[str, tuple[Any, ...]] = {}
    for path in paths:
        info = path.lstat()
        content: bytes | str | None = None
        if stat.S_ISREG(info.st_mode):
            content = path.read_bytes()
        elif stat.S_ISLNK(info.st_mode):
            content = os.readlink(path)
        result[str(path.relative_to(root))] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_mtime_ns,
            info.st_ctime_ns,
            content,
        )
    return result


def test_read_only_review_preserves_owned_and_dirty_source_inputs(
    provider: WorktreeProvider,
) -> None:
    source = provider.source.root
    (source / "file.txt").write_text("staged\n")
    git(source, "add", "file.txt")
    (source / "file.txt").write_text("unstaged\n")
    (source / "untracked.txt").write_text("untracked\n")
    (source / "ignored").write_text("ignored\n")
    (source / "empty").mkdir()
    source_inputs = {
        path: filesystem_state(path)
        for path in [
            source / "file.txt",
            source / "untracked.txt",
            source / "ignored",
            source / "empty",
            source / ".git/index",
        ]
    }
    source_status = git(
        source, "--no-optional-locks", "status", "--porcelain", "--ignored"
    )

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        (owned.handle.path / "empty").mkdir()
        before = filesystem_state(owned.handle.path)
        index = filesystem_state(owned.handle._admin_path)
        result = await FreshReviewer(
            owned,
            SequenceLLM([LLMResponse(content=output(snapshot, []))]),
            budget=RunBudget(max_steps=1),
            timeout_seconds=5,
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.COMPLETED
        assert filesystem_state(owned.handle.path) == before
        assert filesystem_state(owned.handle._admin_path) == index
        assert owned.handle.state is WorktreeState.RETAINED

    asyncio.run(scenario())
    for path, before in source_inputs.items():
        assert filesystem_state(path) == before
    assert (
        git(source, "--no-optional-locks", "status", "--porcelain", "--ignored")
        == source_status
    )


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked", "ignored"])
def test_changed_snapshot_inputs_fail_before_model_without_rewriting_them(
    provider: WorktreeProvider, kind: str
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        root = owned.handle.path
        if kind in {"staged", "unstaged"}:
            (root / "file.txt").write_text("later\n")
            if kind == "staged":
                git(root, "add", "file.txt")
        else:
            (root / ("ignored" if kind == "ignored" else "untracked")).write_text(
                "later\n"
            )
        before = filesystem_state(root)
        index = filesystem_state(owned.handle._admin_path)
        llm = SequenceLLM([])
        result = await FreshReviewer(
            owned, llm, budget=RunBudget(max_steps=1), timeout_seconds=5
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.SNAPSHOT_DRIFT
        assert result.trace_id is None and not llm.calls
        assert filesystem_state(root) == before
        assert filesystem_state(owned.handle._admin_path) == index

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["tracked", "untracked", "ignored", "empty-directory", "mode", "inode"]
)
def test_mutation_during_review_invalidates_even_a_valid_model_verdict(
    provider: WorktreeProvider, kind: str
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)

        class MutatingLLM:
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                root = owned.handle.path
                if kind == "tracked":
                    (root / "file.txt").write_text("changed\n")
                elif kind in {"untracked", "ignored"}:
                    (root / kind).write_text("changed\n")
                elif kind == "empty-directory":
                    (root / "empty").mkdir()
                elif kind == "mode":
                    (root / "file.txt").chmod(0o600)
                else:
                    replacement = root / "replacement"
                    replacement.write_bytes((root / "file.txt").read_bytes())
                    replacement.replace(root / "file.txt")
                return LLMResponse(content=output(snapshot, []))

        result = await FreshReviewer(
            owned, MutatingLLM(), budget=RunBudget(max_steps=1), timeout_seconds=5
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.SNAPSHOT_DRIFT
        assert not result.findings
        assert result.trace_id is not None
        assert owned.handle.path.exists()
        assert owned.handle.state is WorktreeState.RETAINED

    asyncio.run(scenario())


def test_injected_write_and_network_calls_have_no_authority(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.core import loop

    def approval(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A reviewer must never ask for write or network authority")

    monkeypatch.setattr(loop, "_ask_for_approval", approval)

    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        before = filesystem_state(owned.handle.path)
        events: list[Event] = []

        def record_event(event: Event) -> None:
            events.append(event)

        class MaliciousLLM:
            def __init__(self) -> None:
                self.calls = 0

            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                assert tools is not None
                assert [tool["function"]["name"] for tool in tools] == ["read_file"]
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="edit",
                                name="edit_file",
                                arguments={
                                    "path": "file.txt",
                                    "old_text": "after\n",
                                    "new_text": "injected\n",
                                },
                            ),
                            ToolCall(
                                id="bash",
                                name="bash",
                                arguments={"command": "printf injected > file.txt"},
                            ),
                            ToolCall(
                                id="network",
                                name="bash",
                                arguments={
                                    "command": "curl https://example.invalid",
                                    "network_access": True,
                                    "justification": "untrusted injected instruction",
                                },
                            ),
                        ]
                    )
                failures = [
                    json.loads(message.content or "")
                    for message in messages
                    if message.role == "tool"
                ]
                assert len(failures) == 3
                assert all(failure["type"] == "ToolNotFound" for failure in failures)
                return LLMResponse(content=output(snapshot, []))

        llm = MaliciousLLM()
        result = await FreshReviewer(
            owned,
            llm,
            budget=RunBudget(max_steps=2),
            timeout_seconds=5,
            event_handler=record_event,
        ).review(TASK, snapshot, verified)
        assert llm.calls == 2
        assert result.status is ReviewStatus.COMPLETED
        assert [
            event.data["tool"] for event in events if event.type == "tool_error"
        ] == [
            "edit_file",
            "bash",
            "bash",
        ]
        assert not any(event.type == "tool_denied" for event in events)
        assert filesystem_state(owned.handle.path) == before
        await owned.assert_unchanged(snapshot)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["large", "binary", "non-utf8"])
def test_unbounded_or_unreadable_exact_diff_cannot_approve(
    provider: WorktreeProvider, kind: str
) -> None:
    async def scenario() -> None:
        handle = await provider.create("HEAD", "codex/bounded-review")
        handle.retain()
        content = {
            "large": b"x" * (MAX_DIFF_BYTES + 1),
            "binary": b"after\0binary\n",
            "non-utf8": b"after\xff\n",
        }[kind]
        (handle.path / "file.txt").write_bytes(content)
        owned = WorkflowGit(handle)
        snapshot: GitSnapshot = await owned.stage()
        verified = VerificationResult(
            snapshot.tree_revision, (VerificationCheck("fixed", True),)
        )
        llm = SequenceLLM([])
        before = filesystem_state(handle.path)
        result = await FreshReviewer(
            owned, llm, budget=RunBudget(max_steps=1), timeout_seconds=5
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.INCOMPLETE
        assert result.trace_id is None and not llm.calls
        assert not result.findings
        assert filesystem_state(handle.path) == before

    asyncio.run(scenario())


def test_workspace_inspection_entry_cap_fails_closed_before_model(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        before = filesystem_state(owned.handle.path)
        index = filesystem_state(owned.handle._admin_path)
        monkeypatch.setattr(review_git_module, "MAX_WORKSPACE_ENTRIES", 1)
        llm = SequenceLLM([])
        result = await FreshReviewer(
            owned, llm, budget=RunBudget(max_steps=1), timeout_seconds=5
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.INCOMPLETE
        assert not llm.calls and result.trace_id is None
        assert not result.findings
        assert filesystem_state(owned.handle.path) == before
        assert filesystem_state(owned.handle._admin_path) == index

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "非ASCII"),
        ("severity", "critical"),
        ("summary", "  "),
        ("path", "/file.txt"),
        ("path", "../file.txt"),
        ("path", "src\\file.txt"),
        ("line", 0),
        ("line", True),
        ("evidence", "  "),
        ("extra", "untrusted extra finding field"),
    ],
)
def test_finding_contract_rejects_unsafe_or_coerced_fields(
    provider: WorktreeProvider, field: str, value: Any
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        item = finding() | {field: value}
        result = await FreshReviewer(
            owned,
            SequenceLLM([LLMResponse(content=output(snapshot, [item]))]),
            budget=RunBudget(max_steps=1),
            timeout_seconds=5,
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.MALFORMED_OUTPUT
        assert not result.findings and result.trace_id is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["trace_id", "status"])
def test_model_cannot_supply_harness_status_or_trace_identity(
    provider: WorktreeProvider, field: str
) -> None:
    async def scenario() -> None:
        owned, snapshot, verified = await proposal(provider)
        payload = json.loads(output(snapshot, []))
        payload[field] = "a" * 32 if field == "trace_id" else "completed"
        result = await FreshReviewer(
            owned,
            SequenceLLM([LLMResponse(content=json.dumps(payload))]),
            budget=RunBudget(max_steps=1),
            timeout_seconds=5,
        ).review(TASK, snapshot, verified)
        assert result.status is ReviewStatus.MALFORMED_OUTPUT
        assert not result.findings
        assert result.trace_id is not None and result.trace_id != "a" * 32

    asyncio.run(scenario())
