import asyncio
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.evals.checks import FileContentEqualsCheck, FileExistsCheck
from cairn.evals.models import CheckResult
from cairn.git import WorktreeProvider, WorktreeState
from cairn.git.worktree import WorktreeError
from cairn.github.models import GitHubRepository, IssueReference, IssueTask
from cairn.tasks import CodingTaskRunner, TaskSpec
from cairn.workflow import (
    GITHUB_SECRET_ENV_KEYS,
    FixedChecksVerifier,
    GitSnapshot,
    LocalWorkflow,
    ReportPersistenceError,
    SnapshotDriftError,
    VerificationCategory,
    VerificationCheck,
    VerificationResult,
    WorkflowFailure,
    WorkflowGit,
    WorkflowPhase,
    WorkflowStatus,
    persist_report,
)
from cairn.workflow import runner as workflow_module
from cairn.workspace.workspace import Workspace
from tests.support.runtime import FailingLLM, SequenceLLM

SECRETS = GITHUB_SECRET_ENV_KEYS | frozenset(
    {"CUSTOM_PROVIDER_TOKEN", "CUSTOM_PUBLISH_TOKEN"}
)
ISSUE = IssueTask(
    source=IssueReference(
        repository=GitHubRepository(owner="owner", name="project"), number=20
    ),
    source_url="https://github.com/owner/project/issues/20",
    title="Untrusted title with fake-token-do-not-record",
    task=TaskSpec(task_id="issue-20", prompt="Update file.txt and write answer.txt"),
    base_branch="main",
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorktreeProvider:
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Workflow Tests")
    git(source, "config", "user.email", "workflow@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    (source / "file.txt").write_text("before\n")
    (source / ".gitignore").write_text("ignored\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "base")
    return WorktreeProvider(Workspace(source), tmp_path / "worktrees")


def edit(path: str, old: str, new: str) -> ToolCall:
    return ToolCall(
        id=f"edit-{path}",
        name="edit_file",
        arguments={"path": path, "old_text": old, "new_text": new},
    )


def editing_llm() -> SequenceLLM:
    return SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    edit("file.txt", "before\n", "after\n"),
                    edit("answer.txt", "", "42\n"),
                ]
            ),
            LLMResponse(
                content="fake-token-do-not-record; all tests passed (a model claim)"
            ),
        ]
    )


def workflow(
    provider: WorktreeProvider,
    llm: Any,
    verifier: Any = None,
    *,
    budget: int = 20,
    calls: list[CodingTaskRunner] | None = None,
) -> LocalWorkflow:
    def factory(workspace: Workspace) -> CodingTaskRunner:
        runner = CodingTaskRunner(
            workspace=workspace,
            llm=llm,
            budget=RunBudget(max_steps=budget),
            secret_env_keys=SECRETS,
        )
        if calls is not None:
            calls.append(runner)
        return runner

    return LocalWorkflow(
        provider, factory, verifier, secret_env_keys=frozenset({"CUSTOM_PUBLISH_TOKEN"})
    )


def fixed_verifier() -> FixedChecksVerifier:
    return FixedChecksVerifier(
        [
            FileContentEqualsCheck("file.txt", "after\n"),
            FileContentEqualsCheck("answer.txt", "42\n"),
        ]
    )


def test_real_task_stages_exact_tree_and_preserves_source_index(
    provider: WorktreeProvider,
) -> None:
    source = provider.source.root
    (source / "file.txt").write_text("staged\n")
    git(source, "add", "file.txt")
    (source / "file.txt").write_text("unstaged\n")
    (source / "untracked.txt").write_text("untracked\n")
    index = (source / ".git/index").read_bytes()
    before = (
        git(source, "diff"),
        git(source, "diff", "--cached"),
        git(source, "status", "--porcelain"),
    )
    llm = editing_llm()
    runners: list[CodingTaskRunner] = []
    result = asyncio.run(
        workflow(provider, llm, fixed_verifier(), calls=runners).run(
            ISSUE, base_ref="HEAD"
        )
    )
    assert result.report.phase is WorkflowPhase.VERIFIED
    assert result.report.status is WorkflowStatus.VERIFIED
    assert result.handle.state is WorktreeState.RETAINED
    assert len(runners) == 1 and len(llm.calls) == 2
    assert result.snapshot is not None
    assert result.snapshot.changed_files == ("answer.txt", "file.txt")
    assert result.snapshot.tree_revision == git(result.handle.path, "write-tree")
    assert result.verification is not None and result.verification.passed
    assert result.verification.tree_revision == result.snapshot.tree_revision
    assert result.recovery_path.parent == provider.parent
    assert not result.recovery_path.is_relative_to(result.handle.path)
    report = json.loads(result.recovery_path.read_text())
    assert report["repository"] == "owner/project"
    assert report["tree_revision"] == result.snapshot.tree_revision
    assert "fake-token" not in result.recovery_path.read_text()
    assert "final_response" not in report and "error" not in report
    assert result.recovery_path.stat().st_mode & 0o077 == 0
    assert (source / ".git/index").read_bytes() == index
    assert (source / "file.txt").read_text() == "unstaged\n"
    assert before == (
        git(source, "diff"),
        git(source, "diff", "--cached"),
        git(source, "status", "--porcelain"),
    )

    async def release() -> None:
        with pytest.raises(Exception, match="Dirty"):
            await result.handle.release()
        assert result.handle.state is WorktreeState.RETAINED
        await result.handle.release(discard_changes=True)
        assert result.handle.state.value == "released"

    asyncio.run(release())


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "malformed",
        "failed",
        "stale",
        "error",
        "tracked-write",
        "untracked-write",
        "ignored-write",
    ],
)
def test_verification_blocks_invalid_or_changed_tree(
    provider: WorktreeProvider, kind: str
) -> None:
    class Verifier:
        async def verify(self, workspace: Workspace, snapshot: GitSnapshot) -> Any:
            if kind == "malformed":
                return {
                    "tree_revision": snapshot.tree_revision,
                    "checks": [{"passed": True}],
                }
            if kind == "error":
                raise RuntimeError("fake-token-do-not-record")
            if kind == "tracked-write":
                (workspace.root / "file.txt").write_text("unverified\n")
            if kind == "untracked-write":
                (workspace.root / "extra").write_text("unverified\n")
            if kind == "ignored-write":
                (workspace.root / "ignored").write_text("unverified\n")
            return VerificationResult(
                "0" * 40 if kind == "stale" else snapshot.tree_revision,
                (
                    VerificationCheck(
                        "fixed-check",
                        kind != "failed",
                        VerificationCategory.CHECK_FAILED if kind == "failed" else None,
                    ),
                ),
            )

    expected = {
        "missing": WorkflowFailure.MISSING_VERIFIER,
        "malformed": WorkflowFailure.MALFORMED_VERIFICATION,
        "failed": WorkflowFailure.VERIFICATION_FAILED,
        "stale": WorkflowFailure.STALE_VERIFICATION,
        "error": WorkflowFailure.VERIFICATION_ERROR,
        "tracked-write": WorkflowFailure.DRIFT,
        "untracked-write": WorkflowFailure.DRIFT,
        "ignored-write": WorkflowFailure.DRIFT,
    }
    result = asyncio.run(
        workflow(
            provider, editing_llm(), None if kind == "missing" else Verifier()
        ).run(ISSUE, base_ref="HEAD")
    )
    assert result.report.status is WorkflowStatus.BLOCKED
    assert result.report.failure is expected[kind]
    assert result.handle.state is WorktreeState.RETAINED
    assert result.handle.path.exists() and result.recovery_path.exists()
    assert "fake-token" not in result.recovery_path.read_text()


@pytest.mark.parametrize("kind", ["runtime", "budget", "no-diff"])
def test_task_failures_do_not_invoke_verifier(
    provider: WorktreeProvider, kind: str
) -> None:
    class Verifier:
        async def verify(
            self, workspace: Workspace, snapshot: GitSnapshot
        ) -> VerificationResult:
            pytest.fail("A failed or empty task must not invoke verification")

    llm = (
        FailingLLM()
        if kind == "runtime"
        else (
            editing_llm()
            if kind == "budget"
            else SequenceLLM([LLMResponse(content="done")])
        )
    )
    result = asyncio.run(
        workflow(provider, llm, Verifier(), budget=1 if kind == "budget" else 20).run(
            ISSUE, base_ref="HEAD"
        )
    )
    assert (
        result.report.failure
        is {
            "runtime": WorkflowFailure.TASK_RUNTIME_ERROR,
            "budget": WorkflowFailure.TASK_BUDGET_EXHAUSTED,
            "no-diff": WorkflowFailure.NO_CHANGES,
        }[kind]
    )
    assert result.handle.state is WorktreeState.RETAINED
    assert result.verification is None


@pytest.mark.parametrize("wrong_workspace", [False, True])
def test_factory_must_bind_workspace_and_receives_workflow_credentials(
    provider: WorktreeProvider, wrong_workspace: bool
) -> None:
    llm = editing_llm()

    def factory(workspace: Workspace) -> CodingTaskRunner:
        return CodingTaskRunner(
            workspace=provider.source if wrong_workspace else workspace,
            llm=llm,
            budget=RunBudget(max_steps=10),
            secret_env_keys=SECRETS if wrong_workspace else frozenset(),
        )

    result = asyncio.run(
        LocalWorkflow(provider, factory, fixed_verifier()).run(ISSUE, base_ref="HEAD")
    )
    if wrong_workspace:
        assert result.report.failure is WorkflowFailure.RUNNER_CONFIGURATION
        assert not llm.calls
    else:
        assert result.report.status is WorkflowStatus.VERIFIED
        assert llm.calls
    assert result.handle.state is WorktreeState.RETAINED


def test_workflow_guards_replaced_gitfile_before_staging(
    provider: WorktreeProvider,
) -> None:
    async def scenario() -> None:
        handle = await provider.create("HEAD", "codex/owned")
        handle.retain()
        owned_git = WorkflowGit(handle)
        original = (handle.path / ".git").read_bytes()
        source_index = (provider.source.root / ".git/index").read_bytes()
        (handle.path / ".git").write_text(f"gitdir: {provider.source.root / '.git'}\n")
        with pytest.raises(SnapshotDriftError):
            await owned_git.stage()
        assert (provider.source.root / ".git/index").read_bytes() == source_index
        (handle.path / ".git").write_bytes(original)
        await handle.release()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["attributes", "config", "hidden-index", "ignored-input"]
)
def test_staging_rejects_external_filters_and_unverified_inputs(
    provider: WorktreeProvider, kind: str
) -> None:
    async def scenario() -> None:
        handle = await provider.create("HEAD", "codex/filter-test")
        handle.retain()
        owned_git = WorkflowGit(handle)
        (handle.path / "file.txt").write_text("after\n")
        if kind == "attributes":
            (handle.path / ".gitattributes").write_text("* filter=lfs\n")
        elif kind == "config":
            git(handle.path, "config", "filter.bad.clean", "fake-command-never-run")
        elif kind == "hidden-index":
            git(handle.path, "update-index", "--assume-unchanged", "file.txt")
        else:
            (handle.path / "ignored").write_text("not-in-tree")
        with pytest.raises(WorktreeError):
            await owned_git.stage()
        assert handle.path.exists() and handle.state is WorktreeState.RETAINED

    asyncio.run(scenario())


def test_fixed_check_results_and_runtime_schema_are_safe(
    provider: WorktreeProvider,
) -> None:
    class Check:
        name = "fake-token-do-not-record"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            return CheckResult(
                name=self.name, passed=False, message="fake-token", error="fake-token"
            )

    result = asyncio.run(
        workflow(provider, editing_llm(), FixedChecksVerifier([Check()])).run(
            ISSUE, base_ref="HEAD"
        )
    )
    assert result.report.failure is WorkflowFailure.VERIFICATION_FAILED
    assert result.verification is not None
    assert result.verification.checks == (
        VerificationCheck("1:Check", False, VerificationCategory.CHECK_ERROR),
    )
    assert "fake-token" not in result.recovery_path.read_text()
    with pytest.raises(ValueError):
        VerificationCheck("fixed", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        VerificationResult("0" * 40, ())
    with pytest.raises(ValueError):
        VerificationResult(
            "0" * 40, (VerificationCheck("same", True), VerificationCheck("same", True))
        )
    with pytest.raises(FrozenInstanceError):
        result.verification.tree_revision = "1" * 40  # type: ignore[misc]


def test_cooperative_cancel_before_run_retains_without_invoking_factory(
    provider: WorktreeProvider,
) -> None:
    async def scenario() -> None:
        event = asyncio.Event()
        event.set()
        calls: list[CodingTaskRunner] = []
        result = await workflow(
            provider, editing_llm(), fixed_verifier(), calls=calls
        ).run(ISSUE, base_ref="HEAD", cancellation_event=event)
        assert result.report.status is WorkflowStatus.CANCELLED
        assert result.handle.state is WorktreeState.RETAINED
        assert result.recovery_path.exists() and not calls
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["runner", "verifier", "staging"])
def test_cooperative_cancel_covers_every_post_create_phase(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    entered = asyncio.Event()

    class BlockingLLM:
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            entered.set()
            await asyncio.Event().wait()
            return LLMResponse(content="unreachable")

    class BlockingVerifier:
        async def verify(
            self, workspace: Workspace, snapshot: GitSnapshot
        ) -> VerificationResult:
            entered.set()
            await asyncio.Event().wait()
            return VerificationResult(
                snapshot.tree_revision, (VerificationCheck("unreachable", True),)
            )

    async def stage(self: WorkflowGit) -> GitSnapshot:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    if phase == "staging":
        monkeypatch.setattr(WorkflowGit, "stage", stage)

    async def scenario() -> None:
        event = asyncio.Event()
        task = asyncio.create_task(
            workflow(
                provider,
                BlockingLLM() if phase == "runner" else editing_llm(),
                BlockingVerifier(),
            ).run(ISSUE, base_ref="HEAD", cancellation_event=event)
        )
        await entered.wait()
        event.set()
        result = await asyncio.wait_for(task, 5)
        assert result.report.status is WorkflowStatus.CANCELLED
        assert (
            result.handle.state is WorktreeState.RETAINED
            and result.recovery_path.exists()
        )
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    asyncio.run(scenario())


def test_repeated_external_cancel_settles_verifier_and_persists_recovery(
    provider: WorktreeProvider,
) -> None:
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    resume = asyncio.Event()

    class BlockingVerifier:
        async def verify(
            self, workspace: Workspace, snapshot: GitSnapshot
        ) -> VerificationResult:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await resume.wait()
                raise
            return VerificationResult(
                snapshot.tree_revision, (VerificationCheck("unreachable", True),)
            )

    async def scenario() -> None:
        task = asyncio.create_task(
            workflow(provider, editing_llm(), BlockingVerifier()).run(
                ISSUE, base_ref="HEAD"
            )
        )
        await entered.wait()
        task.cancel("first external cancellation")
        await cleaning.wait()
        task.cancel("second external cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        resume.set()
        with pytest.raises(
            asyncio.CancelledError, match="first external cancellation"
        ) as error:
            await task
        assert any("Recovery report:" in note for note in error.value.__notes__)
        reports = list(provider.parent.glob("*.json"))
        assert len(reports) == 1
        report = json.loads(reports[0].read_text())
        assert report["status"] == "cancelled"
        assert Path(report["worktree_path"]).exists()
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    asyncio.run(scenario())


def test_report_write_failure_is_observable_and_retains(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = persist_report

    def fail_final(report: Any, path: Path) -> None:
        if report.phase is WorkflowPhase.VERIFIED:
            raise OSError("fake-token-do-not-record")
        original(report, path)

    monkeypatch.setattr(workflow_module, "persist_report", fail_final)
    with pytest.raises(ReportPersistenceError) as error:
        asyncio.run(
            workflow(provider, editing_llm(), fixed_verifier()).run(
                ISSUE, base_ref="HEAD"
            )
        )
    assert "fake-token" not in str(error.value)
    assert any("Retained worktree:" in note for note in error.value.__notes__)
    assert list(provider.parent.glob("cairn-worktree-*/*"))


def test_github_custom_and_provider_secrets_never_reach_agent_subprocesses(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = provider.source.root / ".cairn/models.toml"
    config.parent.mkdir()
    config.write_text("""[[providers]]
name = "active"
base_url = "https://example.invalid/v1"
api_key_env = "CUSTOM_PROVIDER_TOKEN"
[[providers.models]]
name = "default"
model_ids = ["test/model"]
""")
    git(provider.source.root, "add", ".cairn/models.toml")
    git(provider.source.root, "commit", "-m", "provider")
    for key in SECRETS:
        monkeypatch.setenv(key, "fake-token-do-not-record")
    original = asyncio.create_subprocess_exec
    captured: list[dict[str, str]] = []

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        if args[0] == "git":
            captured.append(dict(kwargs.get("env") or os.environ))
        if args[0] in {"/usr/bin/sandbox-exec", "/usr/bin/bwrap"}:
            captured.append(dict(kwargs["env"]))
            return await original(sys.executable, "-c", "print('safe child')", **kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="bash", name="bash", arguments={"command": "printf safe"}
                    )
                ]
            ),
            *editing_llm().responses,
        ]
    )
    result = asyncio.run(
        workflow(provider, llm, fixed_verifier()).run(ISSUE, base_ref="HEAD")
    )
    assert result.report.status is WorkflowStatus.VERIFIED
    assert captured and all(not (SECRETS & env.keys()) for env in captured)
    assert "fake-token" not in result.recovery_path.read_text()


def test_fixed_check_timeout_is_sanitized(provider: WorktreeProvider) -> None:
    class SlowCheck:
        name = "fake-token-do-not-record"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            await asyncio.Event().wait()
            return CheckResult(name=self.name, passed=True)

    verifier = FixedChecksVerifier(
        [SlowCheck(), FileExistsCheck("answer.txt")], timeout_seconds=0.01
    )
    result = asyncio.run(
        workflow(provider, editing_llm(), verifier).run(ISSUE, base_ref="HEAD")
    )
    assert result.verification is not None
    assert result.verification.checks[0].category is VerificationCategory.CHECK_TIMEOUT
    assert result.verification.checks[1].passed
    assert result.report.status is WorkflowStatus.BLOCKED


@pytest.mark.parametrize("kind", ["same-stat", "index", "head", "branch", "deleted"])
def test_exact_snapshot_detects_drift_after_verification(
    provider: WorktreeProvider, kind: str
) -> None:
    async def scenario() -> None:
        result = await workflow(provider, editing_llm(), fixed_verifier()).run(
            ISSUE, base_ref="HEAD"
        )
        assert result.snapshot is not None
        path = result.handle.path / "file.txt"
        if kind == "same-stat":
            before = path.stat()
            path.write_text(
                "other\n"
            )  # Same size, restored timestamp: bytes still differ.
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif kind == "index":
            path.write_text("other\n")
            git(result.handle.path, "add", "file.txt")
        elif kind == "head":
            git(result.handle.path, "commit", "-m", "unverified commit")
        elif kind == "branch":
            git(result.handle.path, "symbolic-ref", "HEAD", "refs/heads/main")
        else:
            path.unlink()
        with pytest.raises(SnapshotDriftError):
            await WorkflowGit(result.handle).assert_unchanged(result.snapshot)
        assert result.handle.path.exists()

    asyncio.run(scenario())


def test_external_cancel_reports_persistence_failure_without_raw_errors(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    class BlockingVerifier:
        async def verify(
            self, workspace: Workspace, snapshot: GitSnapshot
        ) -> VerificationResult:
            entered.set()
            await asyncio.Event().wait()
            return VerificationResult(
                snapshot.tree_revision, (VerificationCheck("unreachable", True),)
            )

    def fail_cancelled(report: Any, path: Path) -> None:
        if report.status is WorkflowStatus.CANCELLED:
            raise OSError("fake-token-do-not-record")
        persist_report(report, path)

    monkeypatch.setattr(workflow_module, "persist_report", fail_cancelled)

    async def scenario() -> None:
        task = asyncio.create_task(
            workflow(provider, editing_llm(), BlockingVerifier()).run(
                ISSUE, base_ref="HEAD"
            )
        )
        await entered.wait()
        task.cancel("external")
        with pytest.raises(asyncio.CancelledError, match="external") as error:
            await task
        notes = "\n".join(error.value.__notes__)
        assert "could not be persisted" in notes
        assert "Retained worktree:" in notes and "Recovery report:" in notes
        assert "fake-token" not in notes
        assert list(provider.parent.glob("cairn-worktree-*/*"))
        assert not [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]

    asyncio.run(scenario())


def test_fresh_git_rejects_tampered_common_directory(
    provider: WorktreeProvider, tmp_path: Path
) -> None:
    async def scenario() -> None:
        result = await workflow(provider, editing_llm(), fixed_verifier()).run(
            ISSUE, base_ref="HEAD"
        )
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        git(unrelated, "init", "-b", "main")
        git(unrelated, "config", "user.name", "Tests")
        git(unrelated, "config", "user.email", "tests@example.invalid")
        (unrelated / "foreign").write_text("keep")
        git(unrelated, "add", ".")
        git(unrelated, "commit", "-m", "unrelated")
        foreign_index = (unrelated / ".git/index").read_bytes()
        assert result.handle._admin is not None
        common = result.handle._admin[0] / "commondir"
        original = common.read_bytes()
        common.write_text(str(unrelated / ".git") + "\n")
        with pytest.raises(SnapshotDriftError, match="common directory"):
            WorkflowGit(result.handle)
        assert (unrelated / ".git/index").read_bytes() == foreign_index
        common.write_bytes(original)

    asyncio.run(scenario())


def test_git_config_cannot_redirect_owned_staging(
    provider: WorktreeProvider, tmp_path: Path
) -> None:
    async def scenario() -> None:
        handle = await provider.create("HEAD", "codex/worktree-config")
        handle.retain()
        owned = WorkflowGit(handle)
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        git(unrelated, "init", "-b", "main")
        (unrelated / "foreign").write_text("keep")
        git(unrelated, "add", ".")
        foreign_index = (unrelated / ".git/index").read_bytes()
        own_index = handle._admin_path / "index"
        original_index = own_index.read_bytes()
        git(handle.path, "config", "extensions.worktreeConfig", "true")
        git(handle.path, "config", "--worktree", "core.worktree", str(unrelated))
        # A positive control proves real Git honors this retained config.
        assert git(handle.path, "rev-parse", "--show-toplevel") == str(unrelated)
        for operation in (owned.stage(), owned.run("add", "--all", "--", ".")):
            with pytest.raises(SnapshotDriftError, match="redirected"):
                await operation
        assert own_index.read_bytes() == original_index
        assert (unrelated / ".git/index").read_bytes() == foreign_index
        assert (unrelated / "foreign").read_text() == "keep"

    asyncio.run(scenario())
