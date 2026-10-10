"""One Issue, one retained Worktree and one CodingTaskRunner invocation."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from cairn.config import load_project_providers
from cairn.git.worktree import WorktreeError, WorktreeHandle, WorktreeProvider
from cairn.github.models import IssueTask
from cairn.tasks import CodingTaskRunner, TaskResult, TaskStatus
from cairn.workflow.git import NoChangesError, SnapshotDriftError, WorkflowGit
from cairn.workflow.models import (
    GitSnapshot,
    LocalWorkflowResult,
    TaskRepositorySummary,
    TaskResultSummary,
    VerificationResult,
    WorkflowFailure,
    WorkflowPhase,
    WorkflowReport,
    WorkflowStatus,
    persist_report,
)
from cairn.workflow.security import GITHUB_SECRET_ENV_KEYS
from cairn.workflow.verification import DeterministicVerifier
from cairn.workspace.workspace import Workspace


class ReportPersistenceError(RuntimeError):
    """A retained run could not persist its safe recovery metadata."""


@dataclass(slots=True)
class _Progress:
    handle: WorktreeHandle
    report: WorkflowReport
    recovery_path: Path
    snapshot: GitSnapshot | None = None
    verification: VerificationResult | None = None
    task_result: TaskResult | None = None

    def save(self, **updates: Any) -> None:
        self.report = self.report.model_copy(update=updates)
        try:
            persist_report(self.report, self.recovery_path)
        except Exception:
            failure = ReportPersistenceError("Workflow report persistence failed")
            self.annotate(failure)
            raise failure from None

    def annotate(self, error: BaseException) -> None:
        error.add_note(f"Retained worktree: {self.handle.path}")
        error.add_note(f"Recovery report: {self.recovery_path}")
        error.add_note(f"Retained branch: {self.handle.branch}")
        error.add_note(f"Base revision: {self.handle.base_revision}")

    def result(self) -> LocalWorkflowResult:
        return LocalWorkflowResult(
            self.handle,
            self.snapshot,
            self.verification,
            self.report,
            self.recovery_path,
            task_result=self.task_result,
        )


async def _settle(*tasks: asyncio.Task[Any]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    completion = asyncio.gather(*tasks, return_exceptions=True)
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            continue
    completion.result()


class LocalWorkflow:
    """Verification gates later publication; every created worktree is retained.

    The factory constructs a fresh CodingTaskRunner in the supplied Workspace.
    Before running it, this wrapper excludes GitHub, explicitly supplied and
    source-configured credentials. A verifier is selected by the caller, never
    inferred from Issue text.
    """

    def __init__(
        self,
        provider: WorktreeProvider,
        runner_factory: Callable[[Workspace], CodingTaskRunner],
        verifier: DeterministicVerifier | None,
        *,
        secret_env_keys: frozenset[str] = frozenset(),
    ) -> None:
        self.provider = provider
        self.runner_factory = runner_factory
        self.verifier = verifier
        self.secret_env_keys = (
            GITHUB_SECRET_ENV_KEYS | secret_env_keys | provider.secret_env_keys
        )

    async def run(
        self,
        issue: IssueTask,
        *,
        base_ref: str,
        cancellation_event: asyncio.Event | None = None,
    ) -> LocalWorkflowResult:
        worker = asyncio.create_task(self._execute(issue, base_ref, cancellation_event))
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
                result = worker.result()
                progress = _Progress(
                    result.handle,
                    result.report,
                    result.recovery_path,
                    task_result=result.task_result,
                )
                progress.save(
                    status=WorkflowStatus.CANCELLED, failure=WorkflowFailure.CANCELLED
                )
                progress.annotate(primary)
            except asyncio.CancelledError as exc:
                for note in getattr(exc, "__notes__", ()):
                    primary.add_note(note)
            except Exception as exc:
                primary.add_note(f"Workflow cleanup failed: {type(exc).__name__}")
                for note in getattr(exc, "__notes__", ()):
                    primary.add_note(note)
            raise primary

    async def _execute(
        self, issue: IssueTask, base_ref: str, cancellation_event: asyncio.Event | None
    ) -> LocalWorkflowResult:
        catalog = load_project_providers(
            self.provider.source.root / ".cairn/models.toml"
        )
        required_secrets = (
            self.secret_env_keys
            | self.provider.secret_env_keys
            | frozenset(entry.config.api_key_env for entry in catalog.providers)
        )
        # The supplied provider remains the lifecycle owner; tighten its policy.
        self.provider.secret_env_keys |= required_secrets
        branch = f"codex/issue-{issue.source.number}-{uuid4().hex}"
        handle = await self.provider.create(base_ref, branch)
        handle.retain()  # No await may intervene between creation and retention.
        progress = _Progress(
            handle,
            WorkflowReport(
                source_url=issue.source_url,
                issue_number=issue.source.number,
                repository=issue.source.repository.full_name,
                base_branch=issue.base_branch,
                intended_fix=issue.intended_fix,
                branch=branch,
                worktree_path=handle.path,
                base_revision=handle.base_revision,
                head_revision=handle.base_revision,
                phase=WorkflowPhase.CREATED,
                status=WorkflowStatus.BLOCKED,
            ),
            self.provider.parent / f"{handle.path.name}.json",
        )
        progress.save()
        if cancellation_event is not None and cancellation_event.is_set():
            progress.save(
                status=WorkflowStatus.CANCELLED, failure=WorkflowFailure.CANCELLED
            )
            return progress.result()
        steps = asyncio.create_task(
            self._steps(issue, progress, required_secrets, cancellation_event)
        )
        watcher = (
            asyncio.create_task(cancellation_event.wait())
            if cancellation_event is not None
            else None
        )
        owned = [steps] if watcher is None else [steps, watcher]
        primary: BaseException | None = None
        try:
            if watcher is None:
                return await asyncio.shield(steps)
            await asyncio.wait(owned, return_when=asyncio.FIRST_COMPLETED)
            if steps.done():
                return steps.result()
            await _settle(steps)
            progress.save(
                status=WorkflowStatus.CANCELLED, failure=WorkflowFailure.CANCELLED
            )
            return progress.result()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            await _settle(*owned)
            if isinstance(primary, asyncio.CancelledError):
                try:
                    progress.save(
                        status=WorkflowStatus.CANCELLED,
                        failure=WorkflowFailure.CANCELLED,
                    )
                except ReportPersistenceError:
                    primary.add_note(
                        "Cancellation recovery report could not be persisted"
                    )
                progress.annotate(primary)

    async def _steps(
        self,
        issue: IssueTask,
        progress: _Progress,
        required_secrets: frozenset[str],
        cancellation_event: asyncio.Event | None,
    ) -> LocalWorkflowResult:
        try:
            git = WorkflowGit(progress.handle)
            runner = self.runner_factory(progress.handle.workspace)
            if (
                not isinstance(runner, CodingTaskRunner)
                or runner.workspace != progress.handle.workspace
            ):
                progress.save(failure=WorkflowFailure.RUNNER_CONFIGURATION)
                return progress.result()
            runner.secret_env_keys |= required_secrets
            progress.save(phase=WorkflowPhase.RUNNING)
            task_result = await runner.run(
                issue.task, cancellation_event=cancellation_event
            )
            progress.task_result = task_result
            progress.save(
                trace_id=task_result.trace_id,
                task_result=TaskResultSummary(
                    status=task_result.status,
                    repository=TaskRepositorySummary.from_evidence(
                        task_result.repository
                    ),
                ),
            )
            failures = {
                TaskStatus.BUDGET_EXHAUSTED: WorkflowFailure.TASK_BUDGET_EXHAUSTED,
                TaskStatus.RUNTIME_ERROR: WorkflowFailure.TASK_RUNTIME_ERROR,
                TaskStatus.CANCELLED: WorkflowFailure.TASK_CANCELLED,
            }
            if task_result.status is not TaskStatus.COMPLETED:
                progress.save(
                    status=(
                        WorkflowStatus.CANCELLED
                        if task_result.status is TaskStatus.CANCELLED
                        else WorkflowStatus.BLOCKED
                    ),
                    failure=failures.get(
                        task_result.status, WorkflowFailure.TASK_RUNTIME_ERROR
                    ),
                )
                return progress.result()
            progress.save(phase=WorkflowPhase.STAGING)
            progress.snapshot = await git.stage()
            snapshot = progress.snapshot
            progress.save(
                head_revision=snapshot.head_revision,
                tree_revision=snapshot.tree_revision,
                changed_files=snapshot.changed_files,
            )
            if self.verifier is None:
                progress.save(failure=WorkflowFailure.MISSING_VERIFIER)
                return progress.result()
            progress.save(phase=WorkflowPhase.VERIFYING)
            await git.assert_unchanged(snapshot)
            result = await self.verifier.verify(progress.handle.workspace, snapshot)
            if type(result) is not VerificationResult:
                progress.save(failure=WorkflowFailure.MALFORMED_VERIFICATION)
                return progress.result()
            try:
                result.validate()
            except (TypeError, ValueError, AttributeError):
                progress.save(failure=WorkflowFailure.MALFORMED_VERIFICATION)
                return progress.result()
            progress.verification = result
            progress.save(checks=result.checks)
            if result.tree_revision != snapshot.tree_revision:
                progress.save(failure=WorkflowFailure.STALE_VERIFICATION)
                return progress.result()
            await git.assert_unchanged(snapshot)
            if not result.passed:
                progress.save(failure=WorkflowFailure.VERIFICATION_FAILED)
                return progress.result()
            progress.save(phase=WorkflowPhase.VERIFIED, status=WorkflowStatus.VERIFIED)
            return progress.result()
        except asyncio.CancelledError as primary:
            try:
                progress.save(
                    status=WorkflowStatus.CANCELLED, failure=WorkflowFailure.CANCELLED
                )
            except ReportPersistenceError:
                primary.add_note("Cancellation recovery report could not be persisted")
            progress.annotate(primary)
            raise
        except ReportPersistenceError:
            raise
        except NoChangesError:
            progress.save(failure=WorkflowFailure.NO_CHANGES)
        except SnapshotDriftError:
            progress.save(failure=WorkflowFailure.DRIFT)
        except WorktreeError:
            progress.save(failure=WorkflowFailure.GIT_ERROR)
        except Exception:
            progress.save(
                failure=(
                    WorkflowFailure.VERIFICATION_ERROR
                    if progress.report.phase is WorkflowPhase.VERIFYING
                    else WorkflowFailure.TASK_RUNTIME_ERROR
                )
            )
        return progress.result()
