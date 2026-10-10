import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cairn.repo_graph.models import (
    ImportRecord,
    ModuleRecord,
    PythonStatus,
    RelationshipStatus,
    SymbolKind,
    SymbolRecord,
)
from cairn.repo_graph.python import (
    MAX_IMPORT_NAMES,
    MAX_PYTHON_NAME_LENGTH,
    PythonFacts,
    parse_python,
    resolve_dependencies,
)


def parse(
    path: str,
    data: bytes,
    *,
    roots: tuple[str, ...] = (".",),
    max_nodes: int = 1000,
    max_records: int = 1000,
) -> PythonFacts:
    return parse_python(
        path,
        data,
        hashlib.sha256(data).hexdigest(),
        roots=roots,
        max_nodes=max_nodes,
        max_records=max_records,
    )


def test_python_facts_have_syntax_positions_and_source_provenance() -> None:
    data = b"""import sys as system, os
from .helper import first as renamed, second
class Thing:
    import typing
    def method(self):
        def nested():
            pass
        from ..other import third
def function():
    import json
async def asynchronous():
    pass
if False:
    class Conditional:
        pass
"""

    facts = parse("pkg/sub/module.py", data)

    assert facts.module == ModuleRecord(
        "pkg/sub/module.py",
        "pkg.sub.module",
        False,
        PythonStatus.PARSED,
        hashlib.sha256(data).hexdigest(),
    )
    assert [
        (record.name, record.kind, record.line, record.end_line)
        for record in facts.symbols
    ] == [
        ("Thing", SymbolKind.CLASS, 3, 8),
        ("function", SymbolKind.FUNCTION, 9, 10),
        ("asynchronous", SymbolKind.ASYNC_FUNCTION, 11, 12),
    ]
    assert [
        (record.module, record.names, record.level, record.line, record.end_line)
        for record in facts.imports
    ] == [
        ("os", (), 0, 1, 1),
        ("sys", (), 0, 1, 1),
        ("helper", ("first", "second"), 1, 2, 2),
        ("typing", (), 0, 4, 4),
        ("other", ("third",), 2, 8, 8),
        ("json", (), 0, 10, 10),
    ]
    records: tuple[SymbolRecord | ImportRecord, ...] = (*facts.symbols, *facts.imports)
    assert all(record.path == facts.module.path for record in records)
    assert all(record.source_sha256 == facts.module.source_sha256 for record in records)
    assert "system" not in repr(facts)
    assert "renamed" not in repr(facts)


def test_python_source_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    data = f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\nraise RuntimeError("secret")'.encode()

    facts = parse("module.py", data)

    assert facts.module.status is PythonStatus.PARSED
    assert not marker.exists()
    assert "RuntimeError" not in repr(facts)
    assert "secret" not in repr(facts)


def test_python_bytes_honor_pep263_source_encoding() -> None:
    facts = parse(
        "module.py", "# coding: latin-1\nclass Caf\xe9:\n    pass\n".encode("latin-1")
    )

    assert facts.module.status is PythonStatus.PARSED
    assert facts.symbols[0].name == "Café"


@pytest.mark.parametrize(
    ("path", "roots", "name", "is_package"),
    [
        ("module.py", (".",), "module", False),
        ("pkg/__init__.py", (".",), "pkg", True),
        ("pkg/__init__.pyi", (".",), "pkg", True),
        ("pkg/module.pyi", (".",), "pkg.module", False),
        ("src/pkg/module.py", ("src",), "pkg.module", False),
        ("src/pkg/__init__.py", (".", "src"), "pkg", True),
        ("src/vendor/pkg/module.py", ("src", "src/vendor"), "pkg.module", False),
    ],
)
def test_module_identity_uses_explicit_longest_source_root(
    path: str, roots: tuple[str, ...], name: str, is_package: bool
) -> None:
    module = parse(path, b"", roots=roots).module

    assert module.name == name
    assert module.is_package is is_package
    assert module.status is PythonStatus.PARSED


@pytest.mark.parametrize(
    ("path", "roots"),
    [
        ("__init__.py", (".",)),
        ("__init__.pyi", (".",)),
        ("src/__init__.py", ("src",)),
        ("other/module.py", ("src",)),
        ("bad-name.py", (".",)),
        ("class/module.py", (".",)),
        ("pkg/foo.bar.py", (".",)),
        ("../module.py", (".",)),
        ("/module.py", (".",)),
        ("pkg/../module.py", (".",)),
        ("a" * (MAX_PYTHON_NAME_LENGTH + 1) + ".py", (".",)),
        ("/".join(["a" * 100] * 3) + ".py", (".",)),
    ],
)
def test_unsupported_module_identity_keeps_independent_syntax_facts(
    path: str, roots: tuple[str, ...]
) -> None:
    facts = parse(path, b"import os\nclass Safe: pass", roots=roots)

    assert facts.module.status is PythonStatus.UNSUPPORTED
    assert facts.module.name is None
    assert facts.imports[0].module == "os"
    assert facts.symbols[0].name == "Safe"


@pytest.mark.parametrize(
    "data",
    [
        b"def broken(:\n    pass",
        b"value = '\x00'",
        b"\xffinvalid",
        b"# coding: unknown-encoding\npass",
        b"(" * 1000 + b"1" + b")" * 1000,
    ],
)
def test_invalid_python_has_no_parser_diagnostics_or_partial_facts(data: bytes) -> None:
    facts = parse("module.py", data)

    assert facts.module.status is PythonStatus.INVALID
    assert facts.symbols == ()
    assert facts.imports == ()
    assert facts.module.source_sha256 == hashlib.sha256(data).hexdigest()


def test_ast_node_limit_stops_before_later_facts() -> None:
    facts = parse("module.py", b"import os\nimport sys", max_nodes=2)

    assert facts.module.status is PythonStatus.LIMITED
    assert [record.module for record in facts.imports] == ["os"]


def test_record_limit_applies_to_symbols_and_imports_together() -> None:
    facts = parse(
        "module.py", b"class One: pass\nimport os\ndef later(): pass", max_records=2
    )

    assert facts.module.status is PythonStatus.LIMITED
    assert [record.name for record in facts.symbols] == ["One"]
    assert [record.module for record in facts.imports] == ["os"]
    assert len(facts.symbols) + len(facts.imports) == 2


def test_aliases_each_consume_one_import_record() -> None:
    facts = parse("module.py", b"import os, sys, json", max_records=2)

    assert facts.module.status is PythonStatus.LIMITED
    assert [record.module for record in facts.imports] == ["os", "sys"]


def test_zero_record_budget_marks_only_actual_omitted_facts_limited() -> None:
    assert (
        parse("module.py", b"", max_nodes=1, max_records=0).module.status
        is PythonStatus.PARSED
    )
    assert (
        parse("module.py", b"value = 1", max_records=0).module.status
        is PythonStatus.PARSED
    )
    limited = parse("module.py", b"import os", max_records=0)
    assert limited.module.status is PythonStatus.LIMITED
    assert limited.symbols == ()
    assert limited.imports == ()


def test_oversized_import_name_list_is_omitted_as_a_whole() -> None:
    data = (
        "from pkg import "
        + ", ".join(f"name{index}" for index in range(MAX_IMPORT_NAMES + 1))
        + "\nimport os"
    ).encode()

    facts = parse("module.py", data)

    assert facts.module.status is PythonStatus.LIMITED
    assert [record.module for record in facts.imports] == ["os"]


@pytest.mark.parametrize(
    "data",
    [
        ("class " + "a" * (MAX_PYTHON_NAME_LENGTH + 1) + ": pass").encode(),
        ("import " + "a" * (MAX_PYTHON_NAME_LENGTH + 1)).encode(),
        ("from pkg import " + "a" * (MAX_PYTHON_NAME_LENGTH + 1)).encode(),
    ],
)
def test_oversized_fact_names_have_explicit_limits(data: bytes) -> None:
    facts = parse("module.py", data)

    assert facts.module.status is PythonStatus.LIMITED
    assert facts.symbols == ()
    assert facts.imports == ()


def test_python_facts_are_immutable() -> None:
    facts = parse("module.py", b"")

    with pytest.raises(FrozenInstanceError):
        facts.imports = ()  # type: ignore[misc]


def test_dependency_resolution_uses_exact_modules_and_source_provenance() -> None:
    source = parse(
        "src/pkg/module.py",
        b"import pkg.helper\nfrom pkg import helper\nfrom . import helper\nimport sys\nimport unknown_package",
        roots=("src",),
    )
    package = parse("src/pkg/__init__.py", b"", roots=("src",))
    helper = parse("src/pkg/helper.py", b"", roots=("src",))

    dependencies = resolve_dependencies(
        (helper.module, source.module, package.module), source.imports
    )

    assert [
        (record.module, record.target_path, record.status) for record in dependencies
    ] == [
        ("pkg.helper", "src/pkg/helper.py", RelationshipStatus.RESOLVED),
        ("pkg", "src/pkg/__init__.py", RelationshipStatus.RESOLVED),
        ("pkg", "src/pkg/__init__.py", RelationshipStatus.RESOLVED),
        ("sys", None, RelationshipStatus.EXTERNAL),
        ("unknown_package", None, RelationshipStatus.UNRESOLVED),
    ]
    assert all(record.path == source.module.path for record in dependencies)
    assert all(
        record.source_sha256 == source.module.source_sha256 for record in dependencies
    )
    assert [(record.line, record.end_line) for record in dependencies] == [
        (index, index) for index in range(1, 6)
    ]


def test_import_members_do_not_infer_submodule_relationships() -> None:
    source = parse("module.py", b"from pkg import helper")
    helper = parse("pkg/helper.py", b"")

    dependency = resolve_dependencies((source.module, helper.module), source.imports)[0]

    assert dependency.module == "pkg"
    assert dependency.target_path is None
    assert dependency.status is RelationshipStatus.UNRESOLVED


@pytest.mark.parametrize("path", ["pkg/sub/module.py", "pkg/sub/__init__.py"])
def test_relative_imports_use_the_source_package_and_detect_root_escape(
    path: str,
) -> None:
    source = parse(path, b"from ..helper import value\nfrom ...helper import value")
    helper = parse("pkg/helper.py", b"")

    dependencies = resolve_dependencies((source.module, helper.module), source.imports)

    assert dependencies[0].module == "pkg.helper"
    assert dependencies[0].target_path == "pkg/helper.py"
    assert dependencies[0].status is RelationshipStatus.RESOLVED
    assert dependencies[1].module is None
    assert dependencies[1].target_path is None
    assert dependencies[1].status is RelationshipStatus.UNRESOLVED


def test_top_level_relative_imports_cannot_escape_to_the_repository_root() -> None:
    source = parse("module.py", b"from .helper import value")
    helper = parse("helper.py", b"")

    dependency = resolve_dependencies((source.module, helper.module), source.imports)[0]

    assert dependency.status is RelationshipStatus.UNRESOLVED
    assert dependency.target_path is None


def test_duplicate_module_identities_are_ambiguous_including_stubs() -> None:
    source = parse("module.py", b"import pkg.helper")
    implementation = parse("pkg/helper.py", b"")
    stub = parse("pkg/helper.pyi", b"")

    dependency = resolve_dependencies(
        (source.module, implementation.module, stub.module), source.imports
    )[0]

    assert dependency.module == "pkg.helper"
    assert dependency.status is RelationshipStatus.AMBIGUOUS
    assert dependency.target_path is None


@pytest.mark.parametrize(
    "status", [PythonStatus.INVALID, PythonStatus.LIMITED, PythonStatus.UNSUPPORTED]
)
def test_only_parsed_target_modules_can_resolve(status: PythonStatus) -> None:
    source = parse("module.py", b"import target")
    target = ModuleRecord("target.py", "target", False, status, "target-hash")

    dependency = resolve_dependencies((source.module, target), source.imports)[0]

    assert dependency.status is RelationshipStatus.UNRESOLVED
    assert dependency.target_path is None


@pytest.mark.parametrize(
    "status", [PythonStatus.INVALID, PythonStatus.LIMITED, PythonStatus.UNSUPPORTED]
)
def test_incomplete_source_modules_do_not_produce_resolved_relationships(
    status: PythonStatus,
) -> None:
    source = ModuleRecord("module.py", "module", False, status, "source-hash")
    imported = ImportRecord("module.py", "os", (), 0, 1, 1, "source-hash")

    dependency = resolve_dependencies((source,), (imported,))[0]

    assert dependency.status is RelationshipStatus.UNSUPPORTED
    assert dependency.target_path is None


def test_local_exact_module_takes_precedence_over_stdlib_classification() -> None:
    source = parse("module.py", b"import os\nimport xml.etree.ElementTree")
    local = parse("os.py", b"")

    dependencies = resolve_dependencies((source.module, local.module), source.imports)

    assert dependencies[0].status is RelationshipStatus.RESOLVED
    assert dependencies[0].target_path == "os.py"
    assert dependencies[1].status is RelationshipStatus.EXTERNAL


def test_invalid_local_module_is_not_mislabeled_external_or_ignored_as_duplicate() -> (
    None
):
    source = parse("module.py", b"import json")
    invalid = parse("json.py", b"def broken(")
    valid = parse("json.pyi", b"")
    dependency = resolve_dependencies((source.module, invalid.module), source.imports)[
        0
    ]
    assert dependency.status is RelationshipStatus.UNRESOLVED
    assert dependency.target_path is None
    duplicate = resolve_dependencies(
        (source.module, invalid.module, valid.module), source.imports
    )[0]
    assert duplicate.status is RelationshipStatus.AMBIGUOUS
    assert duplicate.target_path is None


def test_missing_source_identity_is_unsupported() -> None:
    imported = ImportRecord("module.py", "os", (), 0, 1, 1, "hash")

    assert (
        resolve_dependencies((), (imported,))[0].status
        is RelationshipStatus.UNSUPPORTED
    )


def test_dependency_order_is_deterministic_across_input_module_order() -> None:
    first = parse("a.py", b"import z\nimport os")
    last = parse("z.py", b"import a")

    expected = resolve_dependencies(
        (first.module, last.module), first.imports + last.imports
    )
    reordered = resolve_dependencies(
        (last.module, first.module), last.imports + first.imports
    )

    assert reordered == expected
    assert [(record.path, record.line, record.module) for record in expected] == [
        ("a.py", 1, "z"),
        ("a.py", 2, "os"),
        ("z.py", 1, "a"),
    ]
