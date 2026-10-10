import asyncio
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    SessionPermissionHandler,
)
from cairn.git import WorktreeProvider, WorktreeState
from cairn.github import publisher as publisher_module
from cairn.github.publisher import GitHubPublisher, PublicationAttemptError
from cairn.github.transport import (
    DraftPR,
    DraftPRPayload,
    GitHubRESTPublisher,
    PublishDestination,
    PublishedCommit,
)
from cairn.repository import RepoContextProvider
from cairn.workflow import (
    LocalWorkflowResult,
    ReportPersistenceError,
    VerificationCheck,
    VerificationResult,
    WorkflowFailure,
    WorkflowPhase,
    WorkflowStatus,
)
from tests.github.test_transport import FakeGitHubAPI
from tests.github.test_transport import git as object_git
from tests.workflow import test_local

provider = test_local.provider


class FakePublisher:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []
        self.commit: PublishedCommit | None = None
        self.payload: DraftPRPayload | None = None
        self.destination: PublishDestination | None = None
        self.path: Path | None = None

    async def verify_destination(self, destination: PublishDestination) -> None:
        self.calls.append("verify")
        self.destination = destination
        if self.failure == "verify":
            raise RuntimeError("fake-publisher-token")
        if self.failure == "drift" and self.path is not None:
            (self.path / "file.txt").write_text("late edit\n")

    async def publish_commit(
        self, destination: PublishDestination, commit: PublishedCommit
    ) -> None:
        self.calls.append("publish")
        self.commit = commit
        if self.failure == "publish":
            raise RuntimeError("fake-publisher-token")

    async def create_draft_pr(self, payload: DraftPRPayload) -> DraftPR:
        self.calls.append("pr")
        self.payload = payload
        if self.failure == "pr":
            raise RuntimeError("fake-publisher-token")
        return DraftPR(
            number=21,
            url="https://github.com/owner/project/pull/21",
            head_revision=payload.commit_revision,
        )


def publisher(transport: FakePublisher, handler: Any = None) -> GitHubPublisher:
    return GitHubPublisher(
        transport,
        permission_handler=handler,
        author_name="Cairn Tests",
        author_email="cairn-tests@example.invalid",
    )


def allow() -> SessionPermissionHandler:
    return SessionPermissionHandler(lambda request: PermissionChoice.ALLOW_ONCE)


def prepare(
    provider: WorktreeProvider, *, intended_fix: bool = False
) -> LocalWorkflowResult:
    issue = test_local.ISSUE.model_copy(update={"intended_fix": intended_fix})
    return asyncio.run(
        test_local.workflow(
            provider, test_local.editing_llm(), test_local.fixed_verifier()
        ).run(issue, base_ref="HEAD")
    )


@pytest.mark.parametrize("intended_fix", [False, True])
def test_publish_exact_verified_commit_and_draft_only(
    provider: WorktreeProvider, intended_fix: bool
) -> None:
    source = provider.source.root
    (source / "file.txt").write_text("staged source\n")
    test_local.git(source, "add", "file.txt")
    (source / "file.txt").write_text("unstaged source\n")
    (source / "untracked-source").write_text("preserve me")
    index = (source / ".git/index").read_bytes()
    result = prepare(provider, intended_fix=intended_fix)
    transport = FakePublisher()
    requests: list[PermissionRequest] = []

    def approve(request: PermissionRequest) -> PermissionChoice:
        requests.append(request)
        return PermissionChoice.ALLOW_ONCE

    issue = test_local.ISSUE.model_copy(update={"intended_fix": intended_fix})
    published = asyncio.run(
        publisher(transport, SessionPermissionHandler(approve)).publish(issue, result)
    )
    assert published.report.status is WorkflowStatus.PUBLISHED
    assert result.task_result is not None
    assert published.task_result is result.task_result
    assert published.report.task_result == result.report.task_result
    assert published.report.phase is WorkflowPhase.PUBLISHED
    assert published.handle.state is WorktreeState.RETAINED
    assert transport.calls == ["verify", "publish", "pr"]
    assert transport.commit is not None and result.snapshot is not None
    commit = transport.commit
    assert test_local.git(result.handle.path, "rev-parse", "HEAD") == commit.sha
    assert (
        test_local.git(result.handle.path, "rev-parse", "HEAD^{tree}")
        == result.snapshot.tree_revision
        == commit.tree
    )
    assert (
        test_local.git(result.handle.path, "rev-parse", "HEAD^")
        == result.handle.base_revision
        == commit.parent
    )
    assert test_local.git(result.handle.path, "status", "--porcelain") == ""
    assert transport.destination is not None and transport.payload is not None
    assert transport.destination.repository.full_name == "owner/project"
    assert transport.destination.base_branch == "main"
    assert transport.destination.head_branch == result.handle.branch
    assert transport.payload.draft is True
    assert transport.payload.commit_revision == commit.sha
    assert ("Closes #20" in transport.payload.body) is intended_fix
    assert result.snapshot.tree_revision in transport.payload.body
    assert "1:FileContentEqualsCheck: PASS" in transport.payload.body
    assert "fake-token" not in transport.payload.body
    assert requests[0].capability is PermissionCapability.NETWORK
    assert requests[0].tool_call.name == "github_publish"
    assert "network_access" not in requests[0].tool_call.arguments
    assert (source / ".git/index").read_bytes() == index
    assert (source / "file.txt").read_text() == "unstaged source\n"
    assert (source / "untracked-source").read_text() == "preserve me"
    stored = json.loads(result.recovery_path.read_text())
    assert stored["remote_published"] is True and stored["commit"] == commit.sha
    assert stored["pr_url"] == "https://github.com/owner/project/pull/21"
    asyncio.run(result.handle.release())
    assert result.handle.remaining_branch == result.handle.branch
    assert (
        test_local.git(source, "rev-parse", f"refs/heads/{result.handle.branch}")
        == commit.sha
    )


@pytest.mark.parametrize("kind", ["none", "deny", "no-grant", "inconsistent-deny"])
def test_permission_denial_has_zero_transport_calls_and_keeps_edits(
    provider: WorktreeProvider, kind: str
) -> None:
    result = prepare(provider)
    transport = FakePublisher()

    class Handler:
        def authorize(self, request: PermissionRequest) -> PermissionResult:
            return PermissionResult(
                policy_decision=PermissionDecision.DENY
                if kind == "inconsistent-deny"
                else PermissionDecision.ASK,
                allowed=True,
                granted_capabilities=frozenset()
                if kind == "no-grant"
                else frozenset({PermissionCapability.NETWORK}),
            )

    handler = (
        None
        if kind == "none"
        else SessionPermissionHandler(lambda request: PermissionChoice.DENY)
        if kind == "deny"
        else Handler()
    )
    blocked = asyncio.run(
        publisher(transport, handler).publish(test_local.ISSUE, result)
    )
    assert blocked.report.failure is WorkflowFailure.PERMISSION_DENIED
    assert blocked.task_result is result.task_result
    assert blocked.report.task_result == result.report.task_result
    assert transport.calls == []
    assert (
        test_local.git(result.handle.path, "rev-parse", "HEAD")
        == result.handle.base_revision
    )
    assert (result.handle.path / "answer.txt").read_text() == "42\n"
    assert result.handle.state is WorktreeState.RETAINED


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "failed",
        "stale",
        "no-diff",
        "blocked",
        "branch-change",
        "intent-change",
    ],
)
def test_invalid_evidence_never_reaches_transport(
    provider: WorktreeProvider, kind: str
) -> None:
    result = prepare(provider)
    assert result.snapshot is not None
    snapshot = result.snapshot
    issue = test_local.ISSUE
    if kind == "missing":
        result = replace(result, verification=None)
    elif kind == "failed":
        result = replace(
            result,
            verification=VerificationResult(
                snapshot.tree_revision, (VerificationCheck("oracle", False),)
            ),
        )
    elif kind == "stale":
        result = replace(
            result,
            verification=VerificationResult(
                "0" * 40, (VerificationCheck("oracle", True),)
            ),
        )
    elif kind == "no-diff":
        snapshot = replace(snapshot)
        object.__setattr__(snapshot, "changed_files", ())
        result = replace(result, snapshot=snapshot)
    elif kind == "blocked":
        result = replace(
            result,
            report=result.report.model_copy(update={"status": WorkflowStatus.BLOCKED}),
        )
    elif kind == "branch-change":
        issue = issue.model_copy(update={"base_branch": "release"})
    else:
        issue = issue.model_copy(update={"intended_fix": True})
    transport = FakePublisher()
    blocked = asyncio.run(publisher(transport, allow()).publish(issue, result))
    assert blocked.report.status is WorkflowStatus.BLOCKED
    assert transport.calls == []


@pytest.mark.parametrize("kind", ["tracked", "untracked", "index", "during-preflight"])
def test_drift_blocks_commit_and_publication(
    provider: WorktreeProvider, kind: str
) -> None:
    result = prepare(provider)
    transport = FakePublisher("drift" if kind == "during-preflight" else None)
    transport.path = result.handle.path
    if kind in {"tracked", "index"}:
        (result.handle.path / "file.txt").write_text("late edit\n")
        if kind == "index":
            test_local.git(result.handle.path, "add", "file.txt")
    elif kind == "untracked":
        (result.handle.path / "late-file").write_text("late edit")
    blocked = asyncio.run(
        publisher(transport, allow()).publish(test_local.ISSUE, result)
    )
    assert blocked.report.failure is WorkflowFailure.DRIFT
    assert transport.calls == (["verify"] if kind == "during-preflight" else [])
    assert (
        test_local.git(result.handle.path, "rev-parse", "HEAD")
        == result.handle.base_revision
    )


@pytest.mark.parametrize("phase", ["verify", "publish", "pr"])
def test_partial_remote_failure_remains_recoverable(
    provider: WorktreeProvider, phase: str
) -> None:
    result = prepare(provider)
    transport = FakePublisher(phase)
    blocked = asyncio.run(
        publisher(transport, allow()).publish(test_local.ISSUE, result)
    )
    assert blocked.report.status is WorkflowStatus.BLOCKED
    assert blocked.report.failure is WorkflowFailure.PUBLISH_FAILED
    assert blocked.handle.state is WorktreeState.RETAINED
    assert blocked.handle.path.is_dir()
    assert "fake-publisher-token" not in blocked.recovery_path.read_text()
    if phase == "verify":
        assert blocked.report.commit is None and transport.calls == ["verify"]
    else:
        assert blocked.report.commit == test_local.git(
            blocked.handle.path, "rev-parse", "HEAD"
        )
        assert blocked.report.remote_branch == blocked.handle.branch
        assert blocked.report.remote_published is (True if phase == "pr" else None)
    assert transport.calls.count("publish") <= 1 and transport.calls.count("pr") <= 1


@pytest.mark.parametrize("phase", ["publish", "pr"])
def test_external_repeated_cancellation_settles_and_retains_recovery(
    provider: WorktreeProvider, phase: str
) -> None:
    result = prepare(provider)

    async def run() -> None:
        started, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Transport(FakePublisher):
            async def pause(self) -> None:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await release.wait()
                    settled.set()

            async def publish_commit(
                self, destination: PublishDestination, commit: PublishedCommit
            ) -> None:
                await super().publish_commit(destination, commit)
                if phase == "publish":
                    await self.pause()

            async def create_draft_pr(self, payload: DraftPRPayload) -> DraftPR:
                if phase == "pr":
                    await self.pause()
                return await super().create_draft_pr(payload)

        operation = asyncio.create_task(
            publisher(Transport(), allow()).publish(test_local.ISSUE, result)
        )
        await asyncio.wait_for(started.wait(), 5)
        operation.cancel("external stop")
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="external stop") as caught:
            await operation
        assert settled.is_set()
        assert str(result.handle.path) in "\n".join(caught.value.__notes__)
        report = json.loads(result.recovery_path.read_text())
        assert report["status"] == "cancelled"
        assert report["commit"] is not None
        assert result.handle.state is WorktreeState.RETAINED
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())


def test_publication_report_write_failure_is_observable(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = prepare(provider)
    transport = FakePublisher()

    def fail(*args: Any) -> None:
        raise OSError("fake-publisher-token")

    monkeypatch.setattr(publisher_module, "persist_report", fail)
    with pytest.raises(ReportPersistenceError) as caught:
        asyncio.run(publisher(transport, allow()).publish(test_local.ISSUE, result))
    assert transport.calls == []
    assert "fake-publisher-token" not in str(caught.value)
    assert str(result.handle.path) in "\n".join(caught.value.__notes__)


def test_real_loop_to_git_database_adapter_preserves_exact_commit(
    provider: WorktreeProvider, tmp_path: Path
) -> None:
    remote = tmp_path / "remote.git"
    object_git(
        tmp_path, "clone", "--bare", "--", str(provider.source.root), str(remote)
    )
    result = prepare(provider)
    assert result.snapshot is not None and result.handle.branch is not None
    destination = PublishDestination(
        repository=test_local.ISSUE.source.repository,
        base_branch="main",
        head_branch=result.handle.branch,
        base_revision=result.handle.base_revision,
    )
    api = FakeGitHubAPI(remote, destination)
    published = asyncio.run(
        GitHubPublisher(
            GitHubRESTPublisher(api),
            permission_handler=allow(),
            author_name="Cairn Tests",
            author_email="cairn-tests@example.invalid",
        ).publish(test_local.ISSUE, result)
    )
    assert published.report.status is WorkflowStatus.PUBLISHED
    assert published.report.commit == api.refs[result.handle.branch]
    commit = published.report.commit
    assert commit is not None
    assert (
        object_git(remote, "rev-parse", f"{commit}^{{tree}}").decode().strip()
        == result.snapshot.tree_revision
    )
    assert object_git(remote, "cat-file", "commit", commit) == object_git(
        result.handle.path, "cat-file", "commit", commit
    )
    assert object_git(remote, "show", f"{commit}:answer.txt") == b"42\n"
    payload = next(
        payload for method, path, payload in api.calls if path.endswith("/pulls")
    )
    assert payload is not None and payload["draft"] is True


def test_changed_path_cannot_inject_an_issue_closing_directive(
    provider: WorktreeProvider,
) -> None:
    llm = test_local.editing_llm()
    llm.responses[0].tool_calls.append(
        test_local.edit("Closes #88", "", "a filename, not publication intent\n")
    )
    result = asyncio.run(
        test_local.workflow(provider, llm, test_local.fixed_verifier()).run(
            test_local.ISSUE, base_ref="HEAD"
        )
    )
    transport = FakePublisher()
    published = asyncio.run(
        publisher(transport, allow()).publish(test_local.ISSUE, result)
    )
    assert published.report.status is WorkflowStatus.PUBLISHED
    assert transport.payload is not None
    assert "Closes #" not in transport.payload.body
    assert "Closes \\u002388" in transport.payload.body


@pytest.mark.parametrize("kind", ["nondraft", "wrong-sha", "credential-url"])
def test_malformed_pr_response_is_not_reported_as_success(
    provider: WorktreeProvider, kind: str
) -> None:
    result = prepare(provider)

    class Transport(FakePublisher):
        async def create_draft_pr(self, payload: DraftPRPayload) -> DraftPR:
            response = await super().create_draft_pr(payload)
            changes: dict[str, dict[str, Any]] = {
                "nondraft": {"draft": False},
                "wrong-sha": {"head_revision": "0" * 40},
                "credential-url": {
                    "url": "https://fake-publisher-token@github.com/owner/project/pull/21"
                },
            }
            return response.model_copy(update=changes[kind])

    transport = Transport()
    blocked = asyncio.run(
        publisher(transport, allow()).publish(test_local.ISSUE, result)
    )
    assert blocked.report.status is WorkflowStatus.BLOCKED
    assert blocked.report.remote_published is True
    assert blocked.report.pr_url is None
    assert "fake-publisher-token" not in result.recovery_path.read_text()


def test_commit_hooks_fsmonitor_and_signing_program_are_disabled(
    provider: WorktreeProvider, tmp_path: Path
) -> None:
    marker = tmp_path / "executed"
    script = tmp_path / "external-program"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    script.chmod(0o755)
    for key, value in (
        ("core.hooksPath", str(tmp_path)),
        ("core.fsmonitor", str(script)),
        ("commit.gpgsign", "true"),
        ("gpg.program", str(script)),
    ):
        test_local.git(provider.source.root, "config", key, value)
    (tmp_path / "reference-transaction").symlink_to(script)
    # Positive control: ordinary interactive context keeps its legacy policy.
    asyncio.run(RepoContextProvider(provider.source).inspect())
    assert marker.exists()
    marker.unlink()
    result = prepare(provider)
    published = asyncio.run(
        publisher(FakePublisher(), allow()).publish(test_local.ISSUE, result)
    )
    assert published.report.status is WorkflowStatus.PUBLISHED
    assert not marker.exists()


@pytest.mark.parametrize("phase", [None, "verify", "publish", "pr"])
def test_reusing_original_result_preserves_publication_recovery(
    provider: WorktreeProvider, phase: str | None
) -> None:
    result = prepare(provider)
    transport = FakePublisher(phase)
    first = asyncio.run(publisher(transport, allow()).publish(test_local.ISSUE, result))
    recovery = result.recovery_path.read_bytes()
    with pytest.raises(PublicationAttemptError):
        asyncio.run(
            publisher(FakePublisher(), allow()).publish(test_local.ISSUE, result)
        )
    assert result.recovery_path.read_bytes() == recovery
    assert json.loads(recovery)["commit"] == first.report.commit
    assert json.loads(recovery)["pr_url"] == first.report.pr_url
    claim = result.recovery_path.with_name(
        result.recovery_path.name + ".publish-attempt"
    )
    assert claim.stat().st_mode & 0o777 == 0o600


def test_concurrent_publishers_cannot_clobber_recovery(
    provider: WorktreeProvider,
) -> None:
    result = prepare(provider)

    async def run() -> None:
        started, resume = asyncio.Event(), asyncio.Event()

        class Transport(FakePublisher):
            async def verify_destination(self, destination: PublishDestination) -> None:
                await super().verify_destination(destination)
                started.set()
                await resume.wait()

        transport = Transport()
        first = asyncio.create_task(
            publisher(transport, allow()).publish(test_local.ISSUE, result)
        )
        await asyncio.wait_for(started.wait(), 5)
        recovery = result.recovery_path.read_bytes()
        other = FakePublisher()
        with pytest.raises(PublicationAttemptError):
            await publisher(other, allow()).publish(test_local.ISSUE, result)
        assert other.calls == [] and result.recovery_path.read_bytes() == recovery
        resume.set()
        published = await first
        assert published.report.status is WorkflowStatus.PUBLISHED
        assert (
            json.loads(result.recovery_path.read_text())["pr_url"]
            == published.report.pr_url
        )
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())


def test_harness_credentials_do_not_reach_actual_git_children(
    provider: WorktreeProvider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    keys = test_local.SECRETS | frozenset({"CAIRN_LLM_API_KEY"})
    for key in keys:
        monkeypatch.setenv(key, "fake-harness-secret-never-forward")
    log = tmp_path / "git-environments.jsonl"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    wrapper = binaries / "git"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"keys = {tuple(sorted(keys))!r}\n"
        f"with open({str(log)!r}, 'a') as stream:\n"
        "    stream.write(json.dumps(sorted(set(keys) & os.environ.keys())) + '\\n')\n"
        f"os.execv({real_git!r}, ['git', *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    result = prepare(provider)
    published = asyncio.run(
        publisher(FakePublisher(), allow()).publish(test_local.ISSUE, result)
    )
    assert published.report.status is WorkflowStatus.PUBLISHED
    environments = [json.loads(line) for line in log.read_text().splitlines()]
    assert environments and all(not present for present in environments)
    assert "fake-harness-secret" not in result.recovery_path.read_text()
    assert all(os.environ[key] == "fake-harness-secret-never-forward" for key in keys)


@pytest.mark.parametrize("binding", ["foreign-branch", "advanced-base"])
def test_snapshot_must_match_original_owned_branch_and_base(
    provider: WorktreeProvider, binding: str
) -> None:
    result = prepare(provider)
    assert result.snapshot is not None
    if binding == "foreign-branch":
        test_local.git(provider.source.root, "branch", "unrelated")
        test_local.git(
            result.handle.path, "symbolic-ref", "HEAD", "refs/heads/unrelated"
        )
        snapshot = replace(result.snapshot, branch="unrelated")
    else:
        test_local.git(result.handle.path, "commit", "-m", "manual advance")
        snapshot = replace(
            result.snapshot,
            head_revision=test_local.git(result.handle.path, "rev-parse", "HEAD"),
        )
    transport = FakePublisher()
    blocked = asyncio.run(
        publisher(transport, allow()).publish(
            test_local.ISSUE, replace(result, snapshot=snapshot)
        )
    )
    assert blocked.report.failure is WorkflowFailure.MALFORMED_VERIFICATION
    assert transport.calls == []
    assert (
        test_local.git(provider.source.root, "rev-parse", "main")
        == result.handle.base_revision
    )
    if binding == "foreign-branch":
        assert (
            test_local.git(provider.source.root, "rev-parse", "unrelated")
            == result.handle.base_revision
        )
