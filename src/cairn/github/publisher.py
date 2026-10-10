"""Harness-only publication of a verified local task; never an Agent tool."""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime

from cairn.core.models import ToolCall
from cairn.core.permissions import (
    CapabilityPermissionHandler,
    PermissionCapability,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
)
from cairn.git.worktree import _run_git
from cairn.github.models import IssueTask
from cairn.github.transport import (
    MAX_NEW_OBJECTS,
    MAX_TOTAL_OBJECT_BYTES,
    DraftPR,
    DraftPRPayload,
    GitHubPublishTransport,
    GitObject,
    PublishDestination,
    PublishedCommit,
    _validate_pr_url,
)
from cairn.workflow.git import SnapshotDriftError, WorkflowGit
from cairn.workflow.models import (
    GitSnapshot,
    LocalWorkflowResult,
    VerificationResult,
    WorkflowFailure,
    WorkflowPhase,
    WorkflowStatus,
    persist_report,
)
from cairn.workflow.runner import ReportPersistenceError


class PublicationAttemptError(RuntimeError):
    """The retained result has already been claimed for publication."""


class GitHubPublisher:
    """An explicitly called publisher, with no retry or automatic cleanup.

    Only in-memory evidence from LocalWorkflow is accepted. Persisted recovery
    reports are diagnostic, not authorization or reusable verification tickets.
    The transport owns authentication; the Agent never receives it.
    """

    def __init__(
        self,
        transport: GitHubPublishTransport,
        *,
        permission_handler: CapabilityPermissionHandler | None,
        author_name: str,
        author_email: str,
    ) -> None:
        for value in (author_name, author_email):
            if (
                not value
                or value != value.strip()
                or any(ord(c) < 32 or ord(c) == 127 or c in "<>" for c in value)
            ):
                raise ValueError("Invalid harness commit identity")
        self.transport = transport
        self.permission_handler = permission_handler
        self.author_name = author_name
        self.author_email = author_email

    async def publish(
        self, issue: IssueTask, result: LocalWorkflowResult
    ) -> LocalWorkflowResult:
        worker = asyncio.create_task(self._publish(issue, result))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as primary:
            if not worker.done():
                worker.cancel()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                worker.result()
            except BaseException as exc:
                for note in getattr(exc, "__notes__", ()):
                    primary.add_note(note)
            primary.add_note(f"Retained worktree: {result.handle.path}")
            primary.add_note(f"Recovery report: {result.recovery_path}")
            raise primary

    async def _publish(
        self, issue: IssueTask, result: LocalWorkflowResult
    ) -> LocalWorkflowResult:
        self._claim_publication(result)
        report = result.report

        def save(**changes: object) -> None:
            nonlocal report
            report = report.model_copy(update=changes)
            try:
                persist_report(report, result.recovery_path)
            except Exception:
                error = ReportPersistenceError("Workflow report persistence failed")
                error.add_note(f"Retained worktree: {result.handle.path}")
                error.add_note(f"Recovery report: {result.recovery_path}")
                raise error from None

        def blocked(failure: WorkflowFailure) -> LocalWorkflowResult:
            save(status=WorkflowStatus.BLOCKED, failure=failure)
            return replace(result, report=report)

        try:
            git = WorkflowGit(result.handle)
            issue = IssueTask.model_validate(issue.model_dump())
            if (
                report.status is not WorkflowStatus.VERIFIED
                or report.source_url != issue.source_url
                or report.issue_number != issue.source.number
                or report.repository.casefold()
                != issue.source.repository.full_name.casefold()
                or report.base_branch != issue.base_branch
                or report.intended_fix is not issue.intended_fix
                or type(result.snapshot) is not GitSnapshot
                or type(result.verification) is not VerificationResult
                or result.snapshot.branch != result.handle.branch
                or result.snapshot.head_revision != result.handle.base_revision
            ):
                return blocked(WorkflowFailure.MALFORMED_VERIFICATION)
            snapshot = result.snapshot
            try:
                result.verification.validate()
            except (AttributeError, TypeError, ValueError):
                return blocked(WorkflowFailure.MALFORMED_VERIFICATION)
            if not result.verification.passed:
                return blocked(WorkflowFailure.VERIFICATION_FAILED)
            if result.verification.tree_revision != snapshot.tree_revision:
                return blocked(WorkflowFailure.STALE_VERIFICATION)
            if not snapshot.changed_files:
                return blocked(WorkflowFailure.NO_CHANGES)
            await git.assert_unchanged(snapshot)
            destination = PublishDestination(
                repository=issue.source.repository,
                base_branch=issue.base_branch,
                head_branch=snapshot.branch,
                base_revision=result.handle.base_revision,
            )
            save(phase=WorkflowPhase.AUTHORIZING)
            request = PermissionRequest(
                capability=PermissionCapability.NETWORK,
                justification=(
                    f"Publish verified tree {snapshot.tree_revision} to "
                    f"{destination.repository.full_name}:{destination.head_branch} "
                    f"and create a Draft PR against {destination.base_branch}."
                ),
                tool_call=ToolCall(
                    id="cairn-publish",
                    name="github_publish",
                    arguments={
                        "repository": destination.repository.full_name,
                        "base": destination.base_branch,
                        "head": destination.head_branch,
                        "tree": snapshot.tree_revision,
                    },
                ),
            )
            if self.permission_handler is None:
                return blocked(WorkflowFailure.PERMISSION_DENIED)
            permission = self.permission_handler.authorize(request)
            if (
                type(permission) is not PermissionResult
                or permission.allowed is not True
                or type(permission.policy_decision) is not PermissionDecision
                or permission.policy_decision is PermissionDecision.DENY
                or type(permission.granted_capabilities) is not frozenset
                or PermissionCapability.NETWORK not in permission.granted_capabilities
                or any(
                    type(capability) is not PermissionCapability
                    for capability in permission.granted_capabilities
                )
            ):
                return blocked(WorkflowFailure.PERMISSION_DENIED)
            await git.assert_unchanged(snapshot)
            await self.transport.verify_destination(destination)
            await git.assert_unchanged(snapshot)
            save(phase=WorkflowPhase.COMMITTING)
            commit = await self._prepare_commit(git, snapshot, issue)
            save(commit=commit.sha)
            await git.assert_unchanged(snapshot)
            await git.run(
                "update-ref",
                "--no-deref",
                "-m",
                "cairn verified task",
                f"refs/heads/{snapshot.branch}",
                commit.sha,
                snapshot.head_revision,
            )
            committed = replace(snapshot, head_revision=commit.sha)
            save(commit=commit.sha, head_revision=commit.sha)
            await git.assert_unchanged(committed)
            save(
                phase=WorkflowPhase.PUBLISHING,
                remote_branch=destination.head_branch,
                remote_published=None,
            )
            await self.transport.publish_commit(destination, commit)
            save(remote_published=True, phase=WorkflowPhase.CREATING_PR)
            await git.assert_unchanged(committed)
            body = self._pr_body(issue, result, commit.sha)
            pr = await self.transport.create_draft_pr(
                DraftPRPayload(
                    destination=destination,
                    commit_revision=commit.sha,
                    title=f"Cairn: address issue #{issue.source.number}",
                    body=body,
                )
            )
            pr = DraftPR.model_validate(pr.model_dump())
            _validate_pr_url(pr.url, destination.repository, pr.number)
            if pr.draft is not True or pr.head_revision != commit.sha:
                return blocked(WorkflowFailure.PUBLISH_FAILED)
            save(
                phase=WorkflowPhase.PUBLISHED,
                status=WorkflowStatus.PUBLISHED,
                failure=None,
                pr_url=pr.url,
            )
            return replace(result, report=report)
        except asyncio.CancelledError as primary:
            try:
                save(status=WorkflowStatus.CANCELLED, failure=WorkflowFailure.CANCELLED)
            except Exception as exc:
                primary.add_note(f"Recovery persistence failed: {type(exc).__name__}")
            primary.add_note(f"Retained worktree: {result.handle.path}")
            primary.add_note(f"Recovery report: {result.recovery_path}")
            raise
        except ReportPersistenceError:
            raise
        except SnapshotDriftError:
            return blocked(WorkflowFailure.DRIFT)
        except Exception:
            return blocked(WorkflowFailure.PUBLISH_FAILED)

    @staticmethod
    def _claim_publication(result: LocalWorkflowResult) -> None:
        # Keep the claim after every outcome. Recovery reports are diagnostics,
        # not retry tickets; another attempt needs fresh local verification.
        path = result.recovery_path.with_name(
            result.recovery_path.name + ".publish-attempt"
        )
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            error: RuntimeError = PublicationAttemptError(
                "Publication already attempted; inspect the retained recovery report"
            )
        except OSError:
            error = ReportPersistenceError("Publication claim persistence failed")
        else:
            os.close(descriptor)
            return
        error.add_note(f"Retained worktree: {result.handle.path}")
        error.add_note(f"Recovery report: {result.recovery_path}")
        raise error from None

    async def _prepare_commit(
        self, git: WorkflowGit, snapshot: GitSnapshot, issue: IssueTask
    ) -> PublishedCommit:
        date = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        message = f"Address issue #{issue.source.number}\n"
        env = git.env
        env.update(
            GIT_AUTHOR_NAME=self.author_name,
            GIT_AUTHOR_EMAIL=self.author_email,
            GIT_AUTHOR_DATE=date,
            GIT_COMMITTER_NAME=self.author_name,
            GIT_COMMITTER_EMAIL=self.author_email,
            GIT_COMMITTER_DATE=date,
        )
        await git.assert_unchanged(snapshot)
        await git.assert_context()
        sha = (
            (
                await _run_git(
                    git.handle.path,
                    "-c",
                    "commit.gpgSign=false",
                    "-c",
                    "i18n.commitEncoding=UTF-8",
                    "commit-tree",
                    snapshot.tree_revision,
                    "-p",
                    snapshot.head_revision,
                    "-m",
                    message,
                    env=env,
                )
            )
            .decode()
            .strip()
        )
        if len(sha) != 40:
            raise ValueError("GitHub publication requires SHA-1 repositories")
        objects: list[GitObject] = []
        object_ids = (
            (
                await git.run(
                    "rev-list",
                    "--objects",
                    "--no-object-names",
                    sha,
                    f"^{snapshot.head_revision}",
                )
            )
            .decode()
            .splitlines()
        )
        if len(object_ids) > MAX_NEW_OBJECTS + 1:
            raise ValueError("Git publication exceeds the object count limit")
        total = 0
        for object_id in object_ids:
            if object_id == sha:
                continue
            kind = (await git.run("cat-file", "-t", object_id)).decode().strip()
            data = await git.run("cat-file", kind, object_id)
            total += len(data)
            if total > MAX_TOTAL_OBJECT_BYTES:
                raise ValueError("Git publication exceeds the object size limit")
            if kind == "blob":
                objects.append(GitObject(object_id, "blob", data))
            elif kind == "tree":
                objects.append(GitObject(object_id, "tree", data))
            else:
                raise ValueError("Unsupported publication object")
        await git.assert_unchanged(snapshot)
        return PublishedCommit(
            sha=sha,
            tree=snapshot.tree_revision,
            parent=snapshot.head_revision,
            message=message,
            author_name=self.author_name,
            author_email=self.author_email,
            date=date,
            objects=tuple(objects),
        )

    @staticmethod
    def _pr_body(issue: IssueTask, result: LocalWorkflowResult, commit: str) -> str:
        assert result.snapshot is not None and result.verification is not None
        checks = "\n".join(
            f"- {check.check_id}: PASS" for check in result.verification.checks
        )
        paths = json.dumps(result.snapshot.changed_files, ensure_ascii=True).replace(
            "#", "\\u0023"
        )
        body = (
            f"Issue source: {issue.source_url}\n\n"
            f"Implementation summary: {len(result.snapshot.changed_files)} verified file changes.\n"
            f"Changed paths (JSON):\n```json\n{paths}\n```\n\n"
            f"Deterministic verification:\n{checks}\n\n"
            f"Verified tree: {result.snapshot.tree_revision}\n"
            f"Commit: {commit}\n"
        )
        if result.report.trace_id is not None:
            body += f"Trace: {result.report.trace_id}\n"
        if issue.intended_fix:
            body += f"\nCloses #{issue.source.number}\n"
        return body
