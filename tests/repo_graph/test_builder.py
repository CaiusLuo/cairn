import hashlib
import os
from pathlib import Path

import pytest

from cairn.repo_graph.builder import DEFAULT_EXCLUSIONS, RepoGraphBuilder
from cairn.repo_graph.models import (
    BuildLimits,
    FileKind,
    FileStatus,
    IssueReason,
    Language,
    ManifestStatus,
    SkipReason,
)
from cairn.workspace.workspace import Workspace


def test_inventory_is_sorted_counted_hashed_and_read_only(tmp_path: Path) -> None:
    (tmp_path / "z.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "view.ts").write_text("const n = 1;\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\ndependencies = ["httpx>=1"]\n', encoding="utf-8"
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    builder = RepoGraphBuilder(Workspace(tmp_path))

    graph = builder.build()
    repeated = builder.build()

    assert graph == repeated
    assert graph.root == tmp_path.resolve()
    assert [file.path for file in graph.files] == [
        "README.md",
        "pyproject.toml",
        "src/view.ts",
        "z.py",
    ]
    assert [directory.path for directory in graph.directories] == [".", "src"]
    assert {record.language: record.file_count for record in graph.languages} == {
        Language.PYTHON: 1,
        Language.TEXT: 1,
        Language.TOML: 1,
        Language.TYPESCRIPT: 1,
    }
    for file in graph.files:
        assert file.status == FileStatus.READABLE
        assert file.source_sha256 == hashlib.sha256(before[file.path]).hexdigest()
        assert file.size_bytes == len(before[file.path])
    assert graph.manifests[0].status == ManifestStatus.PARSED
    assert graph.manifests[0].name == "example"
    assert graph.manifests[0].dependencies == ("httpx",)
    assert graph.report.complete
    assert graph.report.entries_seen == 6
    assert graph.report.bytes_read == sum(map(len, before.values()))
    assert graph.report.skipped == ()
    assert graph.report.issues == ()
    assert builder.snapshot is repeated
    assert builder.last_report is repeated.report
    assert {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_exclusions_are_mandatory_basenames_and_count_as_entries(
    tmp_path: Path,
) -> None:
    excluded = {".git", "node_modules", "generated", ".cairn", "private"}
    for name in excluded:
        (tmp_path / name).mkdir()
        (tmp_path / name / "hidden.py").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / "keep.py").write_text("n = 1", encoding="utf-8")

    graph = RepoGraphBuilder(
        Workspace(tmp_path), exclusions=frozenset({"private"})
    ).build()

    assert [file.path for file in graph.files] == ["keep.py"]
    assert [directory.path for directory in graph.directories] == ["."]
    assert graph.report.entries_seen == 8
    assert {item.reason: item.count for item in graph.report.skipped} == {
        SkipReason.EXCLUDED: 6
    }
    assert graph.report.complete
    assert ".git" in DEFAULT_EXCLUSIONS


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "bad\x00name"])
def test_rejects_exclusion_paths(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="single entry basenames"):
        RepoGraphBuilder(Workspace(tmp_path), exclusions=frozenset({name}))


def test_inside_and_outside_symlinks_are_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret = 1", encoding="utf-8")
    (root / "real.py").write_text("safe = 1", encoding="utf-8")
    (root / "alias.py").symlink_to(root / "real.py")
    (root / "external").symlink_to(outside, target_is_directory=True)
    (root / "loop").symlink_to(root, target_is_directory=True)
    (root / "dangling").symlink_to(root / "missing")

    graph = RepoGraphBuilder(Workspace(root)).build()

    records = {file.path: file for file in graph.files}
    assert set(records) == {"alias.py", "dangling", "external", "loop", "real.py"}
    for name in {"alias.py", "dangling", "external", "loop"}:
        assert records[name].status == FileStatus.SYMLINK
        assert records[name].source_sha256 is None
    assert graph.report.bytes_read == len(b"safe = 1")
    assert graph.report.entries_seen == 6
    assert {item.reason: item.count for item in graph.report.skipped} == {
        SkipReason.SYMLINK: 4
    }
    assert graph.report.complete


def test_special_entries_are_skipped_without_opening(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")

    graph = RepoGraphBuilder(Workspace(tmp_path)).build()

    assert graph.report.complete
    assert graph.files[0].status == FileStatus.SPECIAL
    assert graph.files[0].source_sha256 is None
    assert graph.report.bytes_read == 0
    assert graph.report.skipped[0].reason == SkipReason.SPECIAL


@pytest.mark.parametrize("contents", [b"text\x00binary", b"\xff\xfeinvalid"])
def test_binary_files_are_policy_skips(tmp_path: Path, contents: bytes) -> None:
    (tmp_path / "blob.dat").write_bytes(contents)

    graph = RepoGraphBuilder(Workspace(tmp_path)).build()

    assert graph.report.complete
    assert graph.files[0].status == FileStatus.BINARY
    assert graph.files[0].kind == FileKind.BINARY
    assert graph.files[0].source_sha256 == hashlib.sha256(contents).hexdigest()
    assert graph.report.bytes_read == len(contents)
    assert graph.report.skipped[0].reason == SkipReason.BINARY


def test_python_cookie_bytes_reach_inspector_without_utf8_rejection(
    tmp_path: Path,
) -> None:
    contents = b"# coding: latin-1\nname = 'caf\xe9'\n"
    (tmp_path / "legacy.py").write_bytes(contents)

    class InspectingBuilder(RepoGraphBuilder):
        def _reset_structure(self) -> None:
            super()._reset_structure()
            self.inspected: list[tuple[str, bytes, str]] = []

        def _inspect_python(self, path: str, data: bytes, sha256: str) -> None:
            super()._inspect_python(path, data, sha256)
            self.inspected.append((path, data, sha256))

    builder = InspectingBuilder(Workspace(tmp_path))
    graph = builder.build()

    assert graph.report.complete
    assert graph.files[0].status == FileStatus.READABLE
    assert builder.inspected == [
        ("legacy.py", contents, hashlib.sha256(contents).hexdigest())
    ]
    builder.build()
    assert len(builder.inspected) == 1


def test_entry_limit_discards_the_whole_unsorted_directory(tmp_path: Path) -> None:
    for name in ["z.py", "a.py", "b.py", "c.py"]:
        (tmp_path / name).write_text("n = 1", encoding="utf-8")

    graph = RepoGraphBuilder(
        Workspace(tmp_path), limits=BuildLimits(max_entries=3)
    ).build()

    assert not graph.report.complete
    assert graph.files == ()
    assert graph.report.bytes_read == 0
    assert graph.report.entries_seen == 4
    assert [(issue.reason, issue.path) for issue in graph.report.issues] == [
        (IssueReason.ENTRY_LIMIT, ".")
    ]


def test_nested_entry_limit_stops_traversal_without_arbitrary_subset(
    tmp_path: Path,
) -> None:
    for directory in ["b", "a"]:
        (tmp_path / directory).mkdir()
        for filename in ["second.py", "first.py"]:
            (tmp_path / directory / filename).write_text("n = 1", encoding="utf-8")

    graph = RepoGraphBuilder(
        Workspace(tmp_path), limits=BuildLimits(max_entries=4)
    ).build()

    assert graph.files == ()
    assert [directory.path for directory in graph.directories] == [".", "a", "b"]
    assert graph.report.entries_seen == 5
    assert graph.report.issues[0].path == "a"


def test_depth_limit_is_explicit_and_does_not_open_deeper_contents(
    tmp_path: Path,
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "keep.py").write_text("value = 1", encoding="utf-8")
    (tmp_path / "a" / "b" / "hidden.py").write_text("hidden = 1", encoding="utf-8")

    graph = RepoGraphBuilder(
        Workspace(tmp_path), limits=BuildLimits(max_depth=1)
    ).build()

    assert [file.path for file in graph.files] == ["a/keep.py"]
    assert [directory.path for directory in graph.directories] == [".", "a", "a/b"]
    assert not graph.report.complete
    assert graph.report.entries_seen == 4
    assert [(issue.reason, issue.path) for issue in graph.report.issues] == [
        (IssueReason.DEPTH_LIMIT, "a/b")
    ]


def test_per_file_and_total_byte_limits_have_distinct_statuses(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"123456")
    (tmp_path / "b.py").write_bytes(b"1234")
    (tmp_path / "c.py").write_bytes(b"1234")

    graph = RepoGraphBuilder(
        Workspace(tmp_path), limits=BuildLimits(max_file_bytes=5, max_total_bytes=6)
    ).build()

    assert [(file.path, file.status) for file in graph.files] == [
        ("a.py", FileStatus.OVERSIZED),
        ("b.py", FileStatus.READABLE),
        ("c.py", FileStatus.IO_LIMIT),
    ]
    assert graph.report.bytes_read == 4
    assert not graph.report.complete
    assert {issue.reason for issue in graph.report.issues} == {
        IssueReason.OVERSIZED,
        IssueReason.IO_LIMIT,
    }
    assert graph.files[0].source_sha256 is None
    assert graph.files[2].source_sha256 is None


def test_issue_report_is_bounded(tmp_path: Path) -> None:
    for name in ["d.py", "a.py", "c.py", "b.py"]:
        (tmp_path / name).write_bytes(b"123")

    graph = RepoGraphBuilder(
        Workspace(tmp_path), limits=BuildLimits(max_file_bytes=1, max_issues=2)
    ).build()

    assert not graph.report.complete
    assert [issue.path for issue in graph.report.issues] == ["a.py", "b.py"]
    assert graph.report.omitted_issues == 2


def test_unreadable_file_has_explicit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "locked.py").write_bytes(b"value = 1")
    real_open = os.open

    def denied_open(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        if path == "locked.py":
            raise PermissionError("test unreadable file")
        return real_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", denied_open)

    graph = RepoGraphBuilder(Workspace(tmp_path)).build()

    assert graph.files[0].status == FileStatus.UNREADABLE
    assert graph.files[0].source_sha256 is None
    assert graph.report.bytes_read == 0
    assert not graph.report.complete
    assert graph.report.issues[0].reason == IssueReason.UNREADABLE


def test_mid_read_change_discards_hash_and_marks_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "moving.py"
    file.write_bytes(b"value = 1")
    builder = RepoGraphBuilder(Workspace(tmp_path))
    real_read = builder._read_bytes

    def changing_read(fd: int, size: int) -> bytes:
        result = real_read(fd, size)
        file.write_bytes(b"value = 200")
        return result

    monkeypatch.setattr(builder, "_read_bytes", changing_read)

    graph = builder.build()

    assert graph.files[0].status == FileStatus.CHANGED
    assert graph.files[0].source_sha256 is None
    assert not graph.report.complete
    assert graph.report.bytes_read == len(b"value = 1")
    assert IssueReason.CHANGED in {issue.reason for issue in graph.report.issues}
    assert builder.snapshot is None


def test_short_read_is_explicitly_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "short.py").write_bytes(b"value = 1")
    builder = RepoGraphBuilder(Workspace(tmp_path))
    real_read = os.read

    def short_read(fd: int, count: int) -> bytes:
        return (
            real_read(fd, min(count, 2)) if os.lseek(fd, 0, os.SEEK_CUR) == 0 else b""
        )

    monkeypatch.setattr(os, "read", short_read)

    graph = builder.build()

    assert graph.files[0].status == FileStatus.CHANGED
    assert not graph.report.complete
    assert graph.report.bytes_read == 2


def test_final_verification_detects_post_read_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / "later.py"
    file.write_bytes(b"value = 1")
    builder = RepoGraphBuilder(Workspace(tmp_path))

    def mutate_after_read(path: str, data: bytes, sha256: str) -> None:
        file.write_bytes(b"value = 2")

    monkeypatch.setattr(builder, "_inspect_python", mutate_after_read)

    graph = builder.build()

    assert not graph.report.complete
    assert graph.files[0].status == FileStatus.CHANGED
    assert graph.files[0].source_sha256 is None
    assert [(issue.reason, issue.path) for issue in graph.report.issues] == [
        (IssueReason.CHANGED, "later.py")
    ]


def test_replaced_parent_symlink_is_not_followed_during_final_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "file.py").write_bytes(b"value = 1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_bytes(b"secret = 1")
    builder = RepoGraphBuilder(Workspace(root))

    def replace_parent(path: str, data: bytes, sha256: str) -> None:
        (root / "nested").rename(root / "old")
        (root / "nested").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(builder, "_inspect_python", replace_parent)

    graph = builder.build()

    assert not graph.report.complete
    assert {file.path for file in graph.files} == {"nested/file.py"}
    assert graph.report.bytes_read == len(b"value = 1")
    assert {issue.path for issue in graph.report.issues} >= {".", "nested"}


def test_replaced_root_path_is_detected_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "file.py").write_bytes(b"value = 1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_bytes(b"secret = 1")
    builder = RepoGraphBuilder(Workspace(root))

    def replace_root(path: str, data: bytes, sha256: str) -> None:
        root.rename(tmp_path / "old_root")
        root.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(builder, "_inspect_python", replace_root)

    graph = builder.build()

    assert not graph.report.complete
    assert [file.path for file in graph.files] == ["file.py"]
    assert graph.report.bytes_read == len(b"value = 1")
    assert (IssueReason.CHANGED, ".") in {
        (issue.reason, issue.path) for issue in graph.report.issues
    }


def test_parent_replacement_during_final_verification_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    root = parent / "root"
    root.mkdir(parents=True)
    (root / "source.py").write_bytes(b"value = 1")
    builder = RepoGraphBuilder(Workspace(root))
    real_open = builder._open_root
    calls = 0

    def replacing_open() -> int:
        nonlocal calls
        calls += 1
        fd = real_open()
        if calls == 2:
            parent.rename(tmp_path / "old_parent")
            root.mkdir(parents=True)
            (root / "secret.py").write_bytes(b"secret = 1")
        return fd

    monkeypatch.setattr(builder, "_open_root", replacing_open)

    graph = builder.build()

    assert not graph.report.complete
    assert [file.path for file in graph.files] == ["source.py"]
    assert graph.report.bytes_read == len(b"value = 1")
    assert (IssueReason.CHANGED, ".") in {
        (issue.reason, issue.path) for issue in graph.report.issues
    }


@pytest.mark.parametrize("data", [b"{ broken", b"\xff\x00"])
def test_malformed_manifest_marks_candidate_incomplete(
    tmp_path: Path, data: bytes
) -> None:
    (tmp_path / "package.json").write_bytes(data)

    graph = RepoGraphBuilder(Workspace(tmp_path)).build()

    assert not graph.report.complete
    assert graph.manifests[0].status == ManifestStatus.MALFORMED
    assert graph.report.issues[0].reason == IssueReason.MALFORMED_MANIFEST


def test_manifest_item_limit_marks_candidate_incomplete(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_bytes(
        b'{"name":"sample","dependencies":{"alpha":"1","beta":"2"}}'
    )

    graph = RepoGraphBuilder(
        Workspace(tmp_path), limits=BuildLimits(max_manifest_items=1)
    ).build()

    assert not graph.report.complete
    assert graph.manifests[0].status == ManifestStatus.LIMITED
    assert graph.report.issues[0].reason == IssueReason.MANIFEST_LIMIT


def test_gradle_inventory_is_presence_only_and_complete(tmp_path: Path) -> None:
    (tmp_path / "build.gradle.kts").write_text(
        'plugins { id("java") }\n', encoding="utf-8"
    )

    graph = RepoGraphBuilder(Workspace(tmp_path)).build()

    assert graph.report.complete
    assert graph.manifests[0].status == ManifestStatus.PRESENCE_ONLY


def test_failed_candidate_retains_last_complete_snapshot(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_bytes(b"value = 1")
    builder = RepoGraphBuilder(
        Workspace(tmp_path), limits=BuildLimits(max_file_bytes=12)
    )
    complete = builder.build()
    (tmp_path / "source.py").write_bytes(b"value = 'too many bytes'")

    incomplete = builder.build()

    assert not incomplete.report.complete
    assert incomplete is not complete
    assert builder.snapshot is complete
    assert builder.last_report is incomplete.report
    (tmp_path / "source.py").write_bytes(b"value = 2")
    refreshed = builder.build()
    assert refreshed.report.complete
    assert builder.snapshot is refreshed
    assert builder.last_report is refreshed.report
