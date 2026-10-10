import json
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path

import pytest

from cairn.repo_graph import (
    BuildLimits,
    FileKind,
    FileStatus,
    Language,
    RelationshipStatus,
    RepoGraph,
    RepoGraphBuilder,
    SymbolKind,
)
from cairn.repo_graph.graph import MAX_QUERY_BYTES
from cairn.workspace.workspace import Workspace


@pytest.fixture
def graph(tmp_path: Path) -> RepoGraph:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "helper.py").write_text(
        "async def serve():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "main.py").write_text(
        "from .helper import serve\nimport os\nimport thirdparty\n"
        "class Example:\n    def method(self): pass\n"
        "def run(): pass\n",
        encoding="utf-8",
    )
    (tmp_path / "App.java").write_text("class App {}", encoding="utf-8")
    (tmp_path / "index.ts").write_text("export const x = 1;", encoding="utf-8")
    (tmp_path / "README.md").write_text("Private file contents", encoding="utf-8")
    return RepoGraphBuilder(Workspace(tmp_path)).build()


def test_file_queries_have_stable_order_and_precise_filters(graph: RepoGraph) -> None:
    assert graph.report.complete
    assert [item.path for item in graph.list_files().items] == [
        "App.java",
        "README.md",
        "index.ts",
        "pkg/__init__.py",
        "pkg/helper.py",
        "pkg/main.py",
    ]
    assert [item.path for item in graph.list_files(language=Language.PYTHON).items] == [
        "pkg/__init__.py",
        "pkg/helper.py",
        "pkg/main.py",
    ]
    assert len(graph.list_files(kind=FileKind.SOURCE, path_prefix="pkg").items) == 3
    assert not graph.list_files(path_prefix="pk").items
    assert graph.list_files(path_prefix=".") == graph.list_files()
    assert graph.list_files(limit=1).truncated
    assert not graph.list_files(limit=1).complete


def test_python_symbols_and_dependency_provenance(graph: RepoGraph) -> None:
    result = graph.find_symbols(path="pkg/main.py")
    assert result.complete
    assert [(item.name, item.kind, item.line) for item in result.items] == [
        ("Example", SymbolKind.CLASS, 4),
        ("run", SymbolKind.FUNCTION, 6),
    ]
    assert (
        len(graph.find_symbols(name="serve", kind=SymbolKind.ASYNC_FUNCTION).items) == 1
    )
    assert graph.module_for_path("pkg/__init__.py").items[0].name == "pkg"
    dependencies = graph.dependencies_of("pkg/main.py")
    assert dependencies.complete
    assert [(item.module, item.status) for item in dependencies.items] == [
        ("pkg.helper", RelationshipStatus.RESOLVED),
        ("os", RelationshipStatus.EXTERNAL),
        ("thirdparty", RelationshipStatus.UNRESOLVED),
    ]
    assert dependencies.items[0].target_path == "pkg/helper.py"
    digest = next(
        file.source_sha256 for file in graph.files if file.path == "pkg/main.py"
    )
    assert all(item.source_sha256 == digest for item in result.items)
    assert all(item.source_sha256 == digest for item in dependencies.items)
    assert not graph.dependencies_of("pkg/main.py", limit=1).complete


def test_unsupported_structure_is_explicit_and_bounded(graph: RepoGraph) -> None:
    result = graph.find_symbols()
    assert result.unsupported_paths == ("App.java", "index.ts")
    assert not result.complete
    assert graph.find_symbols(limit=1).truncated
    assert graph.module_for_path("App.java").unsupported_paths == ("App.java",)
    assert graph.dependencies_of("index.ts").unsupported_paths == ("index.ts",)
    assert graph.module_for_path("README.md").unsupported_paths == ("README.md",)
    assert graph.module_for_path("missing.py").complete
    assert not graph.module_for_path("missing.py").items


@pytest.mark.parametrize("limit", [0, -1, 201, True, 1.5])
def test_queries_reject_invalid_limits(graph: RepoGraph, limit: int) -> None:
    for query in (
        graph.list_files,
        graph.find_symbols,
        lambda **kwargs: graph.dependencies_of("pkg/main.py", **kwargs),
    ):
        with pytest.raises(ValueError, match="limit"):
            query(limit=limit)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/tmp/file.py",
        "../secret",
        "pkg/../main.py",
        ".git/config",
        "pkg//main.py",
        "pkg\\main.py",
        "a\0b",
        "./pkg",
        ".",
    ],
)
def test_queries_reject_invalid_paths(graph: RepoGraph, path: str) -> None:
    with pytest.raises(ValueError):
        graph.module_for_path(path)
    with pytest.raises(ValueError):
        graph.dependencies_of(path)
    with pytest.raises(ValueError):
        graph.find_symbols(path=path)
    if path != ".":
        with pytest.raises(ValueError):
            graph.list_files(path_prefix=path)


def test_queries_validate_enum_and_name_filters(graph: RepoGraph) -> None:
    with pytest.raises(ValueError):
        graph.list_files(language="python")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        graph.list_files(kind="source")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        graph.find_symbols(kind="class")  # type: ignore[arg-type]
    for name in ("", "x" * 257):
        with pytest.raises(ValueError):
            graph.find_symbols(name=name)


def test_query_results_are_immutable(graph: RepoGraph) -> None:
    result = graph.list_files()
    with pytest.raises(FrozenInstanceError):
        result.complete = False  # type: ignore[misc]


def test_query_metadata_has_a_hard_byte_bound(graph: RepoGraph) -> None:
    template = graph.files[0]
    large = replace(
        graph,
        files=tuple(
            replace(template, path=f"{i:03}-" + "x" * 8000) for i in range(100)
        ),
    )
    result = large.list_files(limit=200)
    assert result.truncated
    assert not result.complete
    assert (
        len(json.dumps(asdict(result), ensure_ascii=True, separators=(",", ":")))
        <= MAX_QUERY_BYTES
    )
    huge = replace(graph, files=(replace(template, path="x" * MAX_QUERY_BYTES),))
    assert not huge.list_files().items
    assert huge.list_files().truncated


@pytest.mark.parametrize("max_bytes", [1, 10, 100, 4096, 8192])
def test_prompt_summary_is_bounded_and_contains_only_metadata(
    graph: RepoGraph, max_bytes: int
) -> None:
    summary = graph.summary_for_prompt(max_bytes=max_bytes)
    assert len(summary.text.encode("utf-8")) <= max_bytes
    assert "Private file contents" not in summary.text
    assert "def run()" not in summary.text
    assert not summary.complete  # Other source languages are explicitly unsupported.
    if max_bytes == 4096:
        assert "Python AST only" in summary.text
        assert '"Example"' in summary.text


@pytest.mark.parametrize("max_bytes", [0, -1, 8193, True, 1.5])
def test_prompt_summary_rejects_invalid_caps(graph: RepoGraph, max_bytes: int) -> None:
    with pytest.raises(ValueError):
        graph.summary_for_prompt(max_bytes=max_bytes)


def test_incomplete_candidates_cannot_claim_complete_queries(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def a(): pass", encoding="utf-8")
    graph = RepoGraphBuilder(
        Workspace(tmp_path), limits=BuildLimits(max_file_bytes=1)
    ).build()
    assert graph.files[0].status == FileStatus.OVERSIZED
    assert not graph.list_files().complete
    assert not graph.find_symbols().complete
    assert not graph.summary_for_prompt().complete


def test_query_only_uses_snapshot_not_current_files(graph: RepoGraph) -> None:
    (graph.root / "pkg/main.py").unlink()
    assert graph.find_symbols(name="run", path="pkg/main.py").items
    assert graph.list_files() == graph.list_files()


@pytest.mark.parametrize(
    "roots", [(), ("..",), ("/tmp",), (".git",), ("src", "src"), ("./src",)]
)
def test_builder_rejects_invalid_python_roots(
    tmp_path: Path, roots: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        RepoGraphBuilder(Workspace(tmp_path), python_roots=roots)


def test_structure_budget_is_global_and_reported(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def first(): pass", encoding="utf-8")
    (tmp_path / "b.py").write_text("def second(): pass", encoding="utf-8")
    graph = RepoGraphBuilder(
        Workspace(tmp_path), limits=BuildLimits(max_structure_records=1)
    ).build()
    assert not graph.report.complete
    assert [item.name for item in graph.symbols] == ["first"]
    assert not graph.find_symbols().complete
