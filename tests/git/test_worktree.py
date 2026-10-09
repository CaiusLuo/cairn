import asyncio
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest

import cairn.git.worktree as module
from cairn.assembly import build_agent
from cairn.core.loop import run_turn
from cairn.core.models import LLMResponse, ToolCall
from cairn.git import WorktreeProvider
from cairn.git.worktree import WorktreeError, WorktreeHandle, WorktreeState
from cairn.workspace.workspace import Workspace
from tests.git.helpers import assert_removed, git
from tests.support.runtime import TEST_BUDGET, SequenceLLM


def test_revision_branch_agent_and_dirty_source(
    provider: WorktreeProvider,
    source: Workspace,
) -> None:
    (source.root / "file.txt").write_text("staged\n")
    git(source.root, "add", ".")
    (source.root / "file.txt").write_text("unstaged\n")
    (source.root / "untracked").write_text("keep")
    before = (
        git(source.root, "status", "--porcelain"),
        git(source.root, "diff", "--cached"),
    )

    async def scenario() -> None:
        handle = await provider.create("base", "task/one")
        assert handle.path.parent == provider.parent
        assert handle.workspace.root == handle.path
        assert handle.branch == "task/one"
        assert handle.base_revision == git(source.root, "rev-parse", "base")
        assert git(handle.path, "rev-parse", "HEAD") == handle.base_revision
        assert git(handle.path, "branch", "--show-current") == "task/one"
        assert (handle.path / "file.txt").read_text() == "initial\n"
        llm = SequenceLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="edit",
                            name="edit_file",
                            arguments={
                                "path": "answer.txt",
                                "old_text": "",
                                "new_text": "42",
                            },
                        )
                    ]
                ),
                LLMResponse(content="done"),
            ]
        )
        agent = build_agent(
            workspace=handle.workspace,
            llm=llm,
            permission_handler=None,
            event_handler=None,
            tracer=None,
        )
        assert await run_turn(agent, "write answer", budget=TEST_BUDGET) == "done"
        assert (handle.path / "answer.txt").read_text() == "42"
        assert not (source.root / "answer.txt").exists()
        await handle.release(discard_changes=True)
        assert_removed(handle, source)
        assert git(source.root, "branch", "--list", "task/one") == ""
        await handle.release()
        with pytest.raises(WorktreeError, match="Cannot retain"):
            handle.retain()

    asyncio.run(scenario())
    assert before == (
        git(source.root, "status", "--porcelain"),
        git(source.root, "diff", "--cached"),
    )
    assert (source.root / "file.txt").read_text() == "unstaged\n"
    assert (source.root / "untracked").read_text() == "keep"


def test_detached_and_unique_directories(
    provider: WorktreeProvider, source: Workspace
) -> None:
    async def scenario() -> None:
        first = await provider.create("base", None)
        second = await provider.create("HEAD", None)
        assert first.path != second.path
        assert first.branch is None
        assert git(first.path, "branch", "--show-current") == ""
        assert git(first.path, "rev-parse", "HEAD") == git(
            source.root, "rev-parse", "base"
        )
        await first.release()
        await second.release()
        assert_removed(first, source)
        assert_removed(second, source)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "branch", ["main", "bad name", "../escape", "-B", "HEAD", "", "a..b", "@{-1}"]
)
def test_invalid_or_existing_branch(
    provider: WorktreeProvider, source: Workspace, branch: str
) -> None:
    head = git(source.root, "rev-parse", "main")
    with pytest.raises(WorktreeError):
        asyncio.run(provider.create("base", branch))
    assert git(source.root, "rev-parse", "main") == head
    assert len(git(source.root, "worktree", "list").splitlines()) == 1
    assert not provider.parent.exists() or not list(provider.parent.iterdir())


@pytest.mark.parametrize(
    "revision", ["missing", "", "--help", "HEAD:file.txt", "HEAD\0"]
)
def test_invalid_revision(provider: WorktreeProvider, revision: str) -> None:
    with pytest.raises(WorktreeError):
        asyncio.run(provider.create(revision, "new"))
    assert not provider.parent.exists()


def test_invalid_repository_and_parent(source: Workspace, tmp_path: Path) -> None:
    with pytest.raises(WorktreeError):
        asyncio.run(
            WorktreeProvider(Workspace(tmp_path), tmp_path / "dest").create(
                "HEAD", None
            )
        )
    nested = source.root / "nested"
    nested.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(source.root, target_is_directory=True)
    for parent in (source.root, nested / "worktrees", alias / "worktrees"):
        with pytest.raises(WorktreeError, match="outside"):
            asyncio.run(
                WorktreeProvider(Workspace(nested), parent).create("HEAD", None)
            )
    file = tmp_path / "file"
    file.write_text("keep")
    with pytest.raises(FileExistsError):
        asyncio.run(WorktreeProvider(source, file).create("HEAD", None))
    assert file.read_text() == "keep"


def test_path_conflict(
    provider: WorktreeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = uuid4()
    monkeypatch.setattr(module, "uuid4", lambda: token)
    path = provider.parent / f"cairn-worktree-{token.hex}"
    path.mkdir(parents=True)
    (path / "keep").write_text("keep")
    with pytest.raises(FileExistsError):
        asyncio.run(provider.create("HEAD", "new"))
    assert (path / "keep").read_text() == "keep"


@pytest.mark.parametrize("dirty", ["tracked", "staged", "untracked", "ignored"])
def test_dirty_release_and_discard(
    provider: WorktreeProvider, source: Workspace, dirty: str
) -> None:
    async def scenario() -> None:
        handle = await provider.create("HEAD", "task")
        file = handle.path / ("file.txt" if dirty in ("tracked", "staged") else dirty)
        file.write_text("keep")
        if dirty == "staged":
            git(handle.path, "add", ".")
        with pytest.raises(WorktreeError, match="Dirty worktree preserved"):
            await handle.release()
        assert file.read_text() == "keep"
        assert handle.state == WorktreeState.ACTIVE
        assert str(handle.path) in git(source.root, "worktree", "list", "--porcelain")
        await handle.release(discard_changes=True)
        assert_removed(handle, source)

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "exception", "cancel"])
def test_managed_cleanup(
    provider: WorktreeProvider, source: Workspace, outcome: str
) -> None:
    handles: list[WorktreeHandle] = []
    primary = ValueError("body failed")

    async def scenario() -> None:
        async with provider.managed("HEAD", "task") as handle:
            handles.append(handle)
            (handle.path / "new").write_text("ephemeral")
            if outcome == "exception":
                raise primary
            if outcome == "cancel":
                task = asyncio.current_task()
                assert task is not None
                task.cancel("external")
                await asyncio.sleep(0)

    if outcome == "exception":
        with pytest.raises(ValueError) as error:
            asyncio.run(scenario())
        assert error.value is primary
    elif outcome == "cancel":
        with pytest.raises(asyncio.CancelledError, match="external"):
            asyncio.run(scenario())
    else:
        asyncio.run(scenario())
    assert_removed(handles[0], source)
    assert git(source.root, "branch", "--list", "task") == ""


@pytest.mark.parametrize("exceptional", [False, True])
def test_retain_and_explicit_release(
    provider: WorktreeProvider, source: Workspace, exceptional: bool
) -> None:
    async def scenario() -> None:
        handle: WorktreeHandle | None = None
        try:
            async with provider.managed("HEAD", "task") as handle:
                (handle.path / "keep").write_text("retained")
                handle.retain()
                handle.retain()
                if exceptional:
                    raise ValueError("body")
        except ValueError:
            assert exceptional
        assert handle is not None
        assert handle.state == WorktreeState.RETAINED
        assert (handle.path / "keep").read_text() == "retained"
        assert str(handle.path) in git(source.root, "worktree", "list", "--porcelain")
        with pytest.raises(WorktreeError, match="Dirty"):
            await handle.release()
        assert handle.state == WorktreeState.RETAINED
        await handle.release(discard_changes=True)
        assert_removed(handle, source)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["advance", "reset-back", "replace", "checked-out"])
def test_changed_branch_is_preserved(
    provider: WorktreeProvider, source: Workspace, change: str
) -> None:
    async def scenario() -> None:
        handle = await provider.create("base", "task")
        if change in ("advance", "reset-back"):
            (handle.path / "file.txt").write_text("commit")
            git(handle.path, "commit", "-am", "advanced")
            if change == "reset-back":
                git(handle.path, "reset", "--hard", handle.base_revision)
        else:
            git(handle.path, "checkout", "--detach")
            if change == "replace":
                git(source.root, "branch", "-D", "task")
                git(source.root, "branch", "task", "base")
            else:
                git(source.root, "checkout", "task")
        revision = git(source.root, "rev-parse", "task")
        await handle.release()
        assert_removed(handle, source)
        assert handle.remaining_branch == "task"
        assert git(source.root, "rev-parse", "task") == revision

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["branch", "add", "validate"])
def test_partial_failure_rolls_back(
    provider: WorktreeProvider,
    source: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    original = module._git
    failure = RuntimeError("injected creation failure")

    async def fail(
        root: Path, *args: str, env: Mapping[str, str] | None = None
    ) -> bytes:
        if (
            stage == "validate"
            and root.parent == provider.parent
            and args == ("rev-parse", "HEAD")
        ):
            raise failure
        result = await original(root, *args, env=env)
        if (stage == "branch" and args[:2] == ("update-ref", "--create-reflog")) or (
            stage == "add" and args[:2] == ("worktree", "add")
        ):
            raise failure
        return result

    monkeypatch.setattr(module, "_git", fail)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(provider.create("base", "task"))
    assert error.value is failure
    assert not list(provider.parent.iterdir())
    assert git(source.root, "branch", "--list", "task") == ""
    assert len(git(source.root, "worktree", "list").splitlines()) == 1


@pytest.mark.parametrize("primary_kind", ["none", "body", "cancel", "create"])
def test_cleanup_failure_observable(
    provider: WorktreeProvider,
    source: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    primary_kind: str,
) -> None:
    original = module._git
    primary = ValueError("primary")

    async def fail(
        root: Path, *args: str, env: Mapping[str, str] | None = None
    ) -> bytes:
        if args[:2] == ("worktree", "remove"):
            raise OSError("cleanup injection")
        result = await original(root, *args, env=env)
        if primary_kind == "create" and args[:2] == ("worktree", "add"):
            raise primary
        return result

    monkeypatch.setattr(module, "_git", fail)
    handles: list[WorktreeHandle] = []

    async def scenario() -> None:
        async with provider.managed("base", "task") as handle:
            handles.append(handle)
            if primary_kind == "body":
                raise primary
            if primary_kind == "cancel":
                raise asyncio.CancelledError("external")

    expected = (
        OSError
        if primary_kind == "none"
        else asyncio.CancelledError
        if primary_kind == "cancel"
        else ValueError
    )
    with pytest.raises(expected) as error:
        asyncio.run(scenario())
    if primary_kind != "none":
        assert any("cleanup injection" in note for note in error.value.__notes__)
        if primary_kind != "cancel":
            assert error.value is primary
    assert list(provider.parent.iterdir())
    assert "task" in git(source.root, "branch", "--list", "task")
    if handles:
        assert handles[0].state == WorktreeState.ACTIVE
        monkeypatch.setattr(module, "_git", original)
        asyncio.run(handles[0].release())
        assert_removed(handles[0], source)


def test_replaced_directory_and_registration_are_preserved(
    provider: WorktreeProvider, source: Workspace
) -> None:
    async def scenario() -> None:
        handle = await provider.create("base", "task")
        saved = handle.path.with_name("moved")
        handle.path.rename(saved)
        handle.path.mkdir()
        (handle.path / "keep").write_text("unowned")
        with pytest.raises(WorktreeError, match="replaced"):
            await handle.release(discard_changes=True)
        assert (handle.path / "keep").read_text() == "unowned"
        (handle.path / "keep").unlink()
        handle.path.rmdir()
        saved.rename(handle.path)
        gitfile = handle.path / ".git"
        original = gitfile.read_bytes()
        gitfile.write_text("gitdir: /unowned")
        with pytest.raises(WorktreeError, match="registration changed"):
            await handle.release(discard_changes=True)
        gitfile.write_bytes(original)
        await handle.release()
        assert_removed(handle, source)

    asyncio.run(scenario())


def test_missing_directory_cleanup_does_not_prune_unrelated_worktree(
    provider: WorktreeProvider,
    source: Workspace,
    tmp_path: Path,
) -> None:
    import shutil

    unrelated = tmp_path / "unrelated"
    git(source.root, "worktree", "add", "--detach", str(unrelated), "HEAD")
    shutil.rmtree(unrelated)

    async def scenario() -> None:
        handle = await provider.create("base", "task")
        shutil.rmtree(handle.path)
        await handle.release(discard_changes=True)
        assert_removed(handle, source)
        assert str(unrelated) in git(source.root, "worktree", "list", "--porcelain")

    asyncio.run(scenario())


def test_replaced_admin_directory_is_preserved(
    provider: WorktreeProvider,
    source: Workspace,
) -> None:
    async def scenario() -> None:
        handle = await provider.create("base", "task")
        admin = Path(git(handle.path, "rev-parse", "--absolute-git-dir"))
        saved = admin.with_name("saved-admin")
        admin.rename(saved)
        admin.mkdir()
        for name in ("HEAD", "gitdir", "commondir"):
            (admin / name).write_bytes((saved / name).read_bytes())
        with pytest.raises(WorktreeError, match="registration changed"):
            await handle.release(discard_changes=True)
        assert admin.exists() and handle.path.exists()
        for child in admin.iterdir():
            child.unlink()
        admin.rmdir()
        saved.rename(admin)
        await handle.release()
        assert_removed(handle, source)

    asyncio.run(scenario())


def test_bare_source(source: Workspace, tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    git(source.root, "clone", "--bare", str(source.root), str(bare))

    async def scenario() -> None:
        provider = WorktreeProvider(Workspace(bare), tmp_path / "worktrees")
        handle = await provider.create("base", "task")
        assert (handle.path / "file.txt").read_text() == "initial\n"
        await handle.release()
        assert not handle.path.exists()
        assert git(bare, "branch", "--list", "task") == ""

    asyncio.run(scenario())


def test_partial_registration_before_head_is_rolled_back(
    provider: WorktreeProvider,
    source: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = module._git
    primary = OSError("interrupted registration")

    async def fail(
        root: Path, *args: str, env: Mapping[str, str] | None = None
    ) -> bytes:
        if args[:2] == ("worktree", "add"):
            path = Path(args[-2])
            admin = source.root / ".git" / "worktrees" / path.name
            admin.mkdir(parents=True)
            (admin / "gitdir").write_text(str(path / ".git") + "\n")
            raise primary
        return await original(root, *args, env=env)

    monkeypatch.setattr(module, "_git", fail)
    with pytest.raises(OSError) as error:
        asyncio.run(provider.create("base", "task"))
    assert error.value is primary
    assert not getattr(primary, "__notes__", ())
    assert not list(provider.parent.iterdir())
    assert not list((source.root / ".git" / "worktrees").iterdir())
    assert git(source.root, "branch", "--list", "task") == ""


def test_existing_dangling_symbolic_branch_is_not_reused(
    provider: WorktreeProvider,
    source: Workspace,
) -> None:
    git(source.root, "symbolic-ref", "refs/heads/task", "refs/heads/unrelated")
    with pytest.raises(WorktreeError):
        asyncio.run(provider.create("base", "task"))
    assert git(source.root, "symbolic-ref", "refs/heads/task") == "refs/heads/unrelated"
    assert git(source.root, "branch", "--list", "unrelated") == ""
