import asyncio
import hashlib
from pathlib import Path

import pytest

from cairn.git import WorktreeProvider
from cairn.repo_graph import (
    BuildLimits,
    FileStatus,
    IssueReason,
    RelationshipStatus,
    RepoGraphBuilder,
)
from cairn.workspace.workspace import Workspace
from tests.git.conftest import source as source
from tests.git.helpers import git


def test_refresh_replaces_edited_deleted_renamed_and_added_facts(
    tmp_path: Path,
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg/a.py").write_text(
        "from . import b\ndef before(): pass\n", encoding="utf-8"
    )
    (tmp_path / "pkg/b.py").write_text("def deleted(): pass", encoding="utf-8")
    (tmp_path / "old.py").write_text("class Renamed: pass", encoding="utf-8")
    builder = RepoGraphBuilder(Workspace(tmp_path))
    original = builder.build()
    assert original.report.complete

    (tmp_path / "pkg/a.py").write_text(
        "from .c import added\nasync def after(): pass\n", encoding="utf-8"
    )
    (tmp_path / "pkg/b.py").unlink()
    (tmp_path / "old.py").rename(tmp_path / "renamed.py")
    (tmp_path / "pkg/c.py").write_text("def added(): pass", encoding="utf-8")
    refreshed = builder.refresh()

    assert refreshed.report.complete
    assert builder.snapshot is refreshed
    assert builder.last_report is refreshed.report
    assert [record.name for record in refreshed.symbols] == [
        "after",
        "added",
        "Renamed",
    ]
    assert [record.name for record in refreshed.modules] == [
        "pkg",
        "pkg.a",
        "pkg.c",
        "renamed",
    ]
    dependency = refreshed.dependencies_of("pkg/a.py").items[0]
    assert dependency.module == "pkg.c"
    assert dependency.status is RelationshipStatus.RESOLVED
    assert dependency.target_path == "pkg/c.py"
    assert not refreshed.module_for_path("old.py").items
    assert not refreshed.module_for_path("pkg/b.py").items
    assert original.find_symbols(name="before").items
    assert original.module_for_path("old.py").items[0].name == "old"
    assert builder.rebuild() == refreshed == builder.refresh()


@pytest.mark.parametrize(
    "failure", ["oversized", "syntax", "unreadable", "missing_root"]
)
def test_incomplete_refresh_keeps_last_complete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    path = root / "module.py"
    path.write_text("def original(): pass", encoding="utf-8")
    builder = RepoGraphBuilder(Workspace(root), limits=BuildLimits(max_file_bytes=64))
    original = builder.build()
    assert original.report.complete
    if failure == "oversized":
        path.write_text("#" * 65, encoding="utf-8")
    elif failure == "syntax":
        path.write_text("def broken(", encoding="utf-8")
    elif failure == "unreadable":

        def unreadable(fd: int, size: int) -> bytes:
            raise OSError("private diagnostic must not appear")

        monkeypatch.setattr(builder, "_read_bytes", unreadable)
    else:
        root.rename(tmp_path / "moved")

    candidate = builder.refresh()

    assert not candidate.report.complete
    assert builder.snapshot is original
    assert builder.last_report is candidate.report
    assert original.find_symbols(name="original").items
    assert not candidate.find_symbols().complete
    assert "private diagnostic" not in repr(candidate)


def test_unexpected_rebuild_failure_also_preserves_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "module.py").write_text("def original(): pass", encoding="utf-8")
    builder = RepoGraphBuilder(Workspace(tmp_path))
    original = builder.build()

    def failed() -> int:
        raise RuntimeError("injected internal failure")

    monkeypatch.setattr(builder, "_open_root", failed)
    with pytest.raises(RuntimeError, match="injected internal failure"):
        builder.rebuild()
    assert builder.snapshot is original


def test_refresh_keeps_policy_exclusions_and_marks_resource_gaps(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "module.py"
    source.write_text("def visible(): pass", encoding="utf-8")
    builder = RepoGraphBuilder(Workspace(root), limits=BuildLimits(max_file_bytes=64))
    original = builder.build()
    (root / "generated").mkdir()
    (root / "generated/hidden.py").write_text("def hidden(): pass", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("def outside(): pass", encoding="utf-8")
    (root / "outside.py").symlink_to(outside)
    (root / "inside.py").symlink_to(source)
    (root / "binary.py").write_bytes(b"\0binary")
    (root / "oversized.py").write_bytes(b"#" * 65)

    candidate = builder.refresh()

    assert builder.snapshot is original
    assert not candidate.report.complete
    assert {issue.reason for issue in candidate.report.issues} == {
        IssueReason.OVERSIZED
    }
    assert [record.name for record in candidate.symbols] == ["visible"]
    statuses = {record.path: record.status for record in candidate.files}
    assert statuses["inside.py"] is FileStatus.SYMLINK
    assert statuses["outside.py"] is FileStatus.SYMLINK
    assert statuses["binary.py"] is FileStatus.BINARY
    assert statuses["oversized.py"] is FileStatus.OVERSIZED
    assert "generated/hidden.py" not in statuses
    (root / "oversized.py").unlink()
    complete = builder.refresh()
    assert complete.report.complete
    assert builder.snapshot is complete
    assert not complete.find_symbols().complete  # Excluded Python structures explicit.
    assert complete.find_symbols().unsupported_paths == (
        "binary.py",
        "inside.py",
        "outside.py",
    )


def repository_state(
    root: Path,
) -> tuple[str, bytes, str, dict[str, tuple[bytes, int, int]]]:
    status = git(
        root,
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored",
    )
    files = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts or path.is_symlink() or not path.is_file():
            continue
        metadata = path.stat()
        files[relative.as_posix()] = (
            path.read_bytes(),
            metadata.st_mode,
            metadata.st_mtime_ns,
        )
    return (
        git(root, "rev-parse", "HEAD"),
        (root / ".git/index").read_bytes(),
        status,
        files,
    )


def test_build_and_queries_preserve_source_git_head_index_and_all_file_states(
    source: Workspace,
) -> None:
    root = source.root
    (root / "module.py").write_text("def staged(): pass", encoding="utf-8")
    git(root, "add", "module.py")
    (root / "module.py").write_text("def unstaged(): pass", encoding="utf-8")
    (root / "untracked.py").write_text("class Untracked: pass", encoding="utf-8")
    (root / "ignored").mkdir()
    (root / "ignored/secret.txt").write_text("ignored fixture", encoding="utf-8")
    before = repository_state(root)

    builder = RepoGraphBuilder(source)
    graph = builder.build()
    assert graph.report.complete
    assert graph.find_symbols(name="unstaged").items
    assert not graph.find_symbols(name="staged").items
    graph.list_files()
    graph.module_for_path("module.py")
    graph.dependencies_of("module.py")
    graph.summary_for_prompt()
    assert builder.refresh() == graph
    assert builder.rebuild() == graph

    assert repository_state(root) == before
    record = graph.list_files(path_prefix="module.py").items[0]
    assert record.source_sha256 == hashlib.sha256(b"def unstaged(): pass").hexdigest()


def test_worktrees_have_independent_snapshots(
    source: Workspace, tmp_path: Path
) -> None:
    async def exercise() -> None:
        provider = WorktreeProvider(source, tmp_path / "worktrees")
        first = await provider.create("HEAD", "repo-graph-first")
        second = await provider.create("HEAD", "repo-graph-second")
        try:
            (first.path / "module.py").write_text("class First: pass", encoding="utf-8")
            (second.path / "module.py").write_text(
                "class Second: pass", encoding="utf-8"
            )
            first_builder = RepoGraphBuilder(first.workspace)
            second_builder = RepoGraphBuilder(second.workspace)
            one = first_builder.build()
            two = second_builder.build()
            assert one.report.complete and two.report.complete
            assert [symbol.name for symbol in one.symbols] == ["First"]
            assert [symbol.name for symbol in two.symbols] == ["Second"]
            assert one.root != two.root
            (first.path / "module.py").write_text(
                "class Updated: pass", encoding="utf-8"
            )
            updated = first_builder.refresh()
            assert [symbol.name for symbol in updated.symbols] == ["Updated"]
            assert second_builder.refresh() == two
            assert [symbol.name for symbol in one.symbols] == ["First"]
            assert not (source.root / "module.py").exists()
        finally:
            await first.release(discard_changes=True)
            await second.release(discard_changes=True)

    asyncio.run(exercise())
