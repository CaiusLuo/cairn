import hashlib
import json
from pathlib import Path

import pytest

from cairn.repo_graph.manifests import MAX_NAME_LENGTH, parse_manifest
from cairn.repo_graph.models import ManifestKind, ManifestStatus


@pytest.mark.parametrize(
    ("path", "data", "kind", "name", "dependencies"),
    [
        (
            "tools/pyproject.toml",
            b"""[project]
name = "Example_Package"
dependencies = ["Requests[security]>=2.31", "Foo.Bar @ https://example.test/foo.whl"]
[project.optional-dependencies]
test = ["pytest>=8; python_version >= '3.12'", "requests==2.31"]
""",
            ManifestKind.PYTHON,
            "example-package",
            ("foo-bar", "pytest", "requests"),
        ),
        (
            "web/package.json",
            b"""{"name":"@example/web","dependencies":{"zod":"^4","@scope/lib":"workspace:*"},
"devDependencies":{"typescript":"^5"},"peerDependencies":{"zod":"*"},
"optionalDependencies":{"optional-lib":"1"},
"scripts":{"postinstall":"do not expose or run this"}}""",
            ManifestKind.NODE,
            "@example/web",
            ("@scope/lib", "optional-lib", "typescript", "zod"),
        ),
        (
            "java/pom.xml",
            b"""<project xmlns="http://maven.apache.org/POM/4.0.0">
<parent><groupId>org.example</groupId><artifactId>parent</artifactId></parent>
<artifactId>app</artifactId><name>Not the package coordinate</name>
<dependencies><dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId>
<version>2.0</version></dependency></dependencies>
</project>""",
            ManifestKind.JAVA,
            "org.example:app",
            ("org.slf4j:slf4j-api",),
        ),
        (
            "go.mod",
            b"""module example.test/app
go 1.23.0
toolchain go1.23.1
require example.test/single v1.2.3 // indirect
require (
 example.test/a v0.1.0
 example.test/z/v2 v2.0.0-beta.1
)
""",
            ManifestKind.GO,
            "example.test/app",
            ("example.test/a", "example.test/single", "example.test/z/v2"),
        ),
        (
            "rust/Cargo.toml",
            b"""[package]
name = "example-app"
[dependencies]
serde = { version = "1", features = ["derive"] }
local-lib = { path = "../local", package = "actual-package" }
[dev-dependencies]
tokio = "1"
[build-dependencies]
cc = "1"
""",
            ManifestKind.RUST,
            "example-app",
            ("cc", "local-lib", "serde", "tokio"),
        ),
    ],
)
def test_extracts_only_safe_manifest_metadata(
    path: str,
    data: bytes,
    kind: ManifestKind,
    name: str,
    dependencies: tuple[str, ...],
) -> None:
    record = parse_manifest(path, data, limit=20)

    assert record.path == path
    assert record.kind is kind
    assert record.status is ManifestStatus.PARSED
    assert record.source_sha256 == hashlib.sha256(data).hexdigest()
    assert record.name == name
    assert record.dependencies == dependencies
    assert record.truncated is False
    assert "do not expose" not in repr(record)


@pytest.mark.parametrize("basename", ["build.gradle", "build.gradle.kts"])
def test_gradle_records_presence_without_executing_build_code(
    tmp_path: Path, basename: str
) -> None:
    marker = tmp_path / "executed"
    data = f'new File("{marker}").write("executed")\n\xff'.encode()

    record = parse_manifest(basename, data, limit=1)

    assert record.kind is ManifestKind.GRADLE
    assert record.status is ManifestStatus.PRESENCE_ONLY
    assert record.name is None
    assert record.dependencies == ()
    assert record.source_sha256 == hashlib.sha256(data).hexdigest()
    assert record.truncated is False
    assert not marker.exists()


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("pyproject.toml", b"[project"),
        ("pyproject.toml", b"[project]\nname = 1"),
        ("pyproject.toml", b'[project]\ndependencies = "requests"'),
        ("pyproject.toml", b"[project]\ndependencies = [1]"),
        ("pyproject.toml", b'[project]\ndependencies = ["not a requirement"]'),
        ("pyproject.toml", b'[project]\ndependencies = ["requests; "]'),
        ("pyproject.toml", b'[project]\ndependencies = ["requests(>=1"]'),
        ("pyproject.toml", b'[project]\ndependencies = ["requests>=1)"]'),
        ("pyproject.toml", b'[project.optional-dependencies]\ntest = "pytest"'),
        ("pyproject.toml", b'project = "wrong shape"'),
        ("package.json", b"{"),
        ("package.json", b"[]"),
        ("package.json", b'{"name":null}'),
        ("package.json", b'{"name":"unsafe\\nname"}'),
        ("package.json", b'{"dependencies":["safe"]}'),
        ("package.json", b'{"dependencies":{"safe":{}}}'),
        ("package.json", b'{"optionalDependencies":["safe"]}'),
        ("package.json", b'{"dependencies":{"../escape":"1"}}'),
        ("package.json", b'{"irrelevant":NaN}'),
        ("package.json", b'{"irrelevant":Infinity}'),
        ("package.json", b'{"irrelevant":-Infinity}'),
        ("pom.xml", b"<project>"),
        ("pom.xml", b"<other/>"),
        ("pom.xml", b"<project><groupId>org.example</groupId></project>"),
        ("pom.xml", b"<project><dependencies><dependency/></dependencies></project>"),
        ("pom.xml", b"<project><groupId><nested/></groupId></project>"),
        ("go.mod", b"go 1.23"),
        ("go.mod", b"module example.test/app\nrequire (\nexample.test/a v1.0.0"),
        ("go.mod", b"module example.test/app\nrequire example.test/a"),
        ("go.mod", b"module example.test/app\nrequire example.test/a invalid"),
        ("go.mod", b"module ../escape"),
        ("Cargo.toml", b"[package"),
        ("Cargo.toml", b'package = "wrong shape"'),
        ("Cargo.toml", b"[package]\nname = 1"),
        ("Cargo.toml", b"[dependencies]\nsafe = 1"),
        ("Cargo.toml", b"dependencies = []"),
    ],
)
def test_malformed_syntax_and_shapes_return_no_unrestricted_diagnostics(
    path: str, data: bytes
) -> None:
    record = parse_manifest(path, data, limit=10)

    assert record.status is ManifestStatus.MALFORMED
    assert record.name is None
    assert record.dependencies == ()
    assert record.source_sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "data",
    [
        b'<!DOCTYPE project [<!ENTITY secret "sensitive">]><project>&secret;</project>',
        b'<!DOCTYPE project SYSTEM "file:///etc/passwd"><project/>',
        '<!DOCTYPE project [<!ENTITY a "x">]><project>&a;</project>'.encode("utf-16"),
    ],
)
def test_maven_rejects_document_types_and_entities(data: bytes) -> None:
    record = parse_manifest("pom.xml", data, limit=10)

    assert record.status is ManifestStatus.MALFORMED
    assert record.dependencies == ()
    assert record.name is None


@pytest.mark.parametrize(
    "path", ["pyproject.toml", "package.json", "pom.xml", "go.mod", "Cargo.toml"]
)
def test_invalid_bytes_are_typed_failures(path: str) -> None:
    assert (
        parse_manifest(path, b"\xff\xfeinvalid", limit=10).status
        is ManifestStatus.MALFORMED
    )


@pytest.mark.parametrize("names", [("z", "b", "a", "a"), ("a", "z", "b", "a")])
def test_dependency_limit_is_sorted_deduplicated_and_order_independent(
    names: tuple[str, ...],
) -> None:
    data = json.dumps({"dependencies": dict.fromkeys(names, "1")}).encode()
    record = parse_manifest("package.json", data, limit=2)

    assert record.dependencies == ("a", "b")
    assert record.status is ManifestStatus.LIMITED
    assert record.truncated is True


def test_duplicate_dependencies_do_not_consume_the_item_limit() -> None:
    record = parse_manifest(
        "pyproject.toml",
        b'[project]\ndependencies = ["Foo_Bar>=1", "foo-bar==2"]',
        limit=1,
    )

    assert record.dependencies == ("foo-bar",)
    assert record.status is ManifestStatus.PARSED
    assert record.truncated is False


def test_long_names_are_omitted_with_an_explicit_limit_status() -> None:
    too_long = "a" * (MAX_NAME_LENGTH + 1)
    data = json.dumps(
        {"name": too_long, "dependencies": {too_long: "1", "safe": "1"}}
    ).encode()

    record = parse_manifest("package.json", data, limit=10)

    assert record.name is None
    assert record.dependencies == ("safe",)
    assert record.status is ManifestStatus.LIMITED
    assert record.truncated is True


def test_deep_maven_metadata_is_limited() -> None:
    data = b"<project>" + b"<x>" * 65 + b"</x>" * 65 + b"</project>"

    record = parse_manifest("pom.xml", data, limit=10)

    assert record.status is ManifestStatus.LIMITED
    assert record.truncated is True
    assert record.dependencies == ()


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("pyproject.toml", b'[build-system]\nrequires = ["setuptools"]'),
        ("package.json", b"{}"),
        ("pom.xml", b"<project/>"),
        ("Cargo.toml", b'[workspace]\nmembers = ["one", "two"]'),
    ],
)
def test_valid_manifests_without_package_metadata_remain_inventory_facts(
    path: str, data: bytes
) -> None:
    record = parse_manifest(path, data, limit=10)

    assert record.status is ManifestStatus.PARSED
    assert record.name is None
    assert record.dependencies == ()


def test_build_system_and_script_fields_are_not_dependency_or_name_facts() -> None:
    data = b"""[build-system]
requires = ["backend"]
build-backend = "code_that_must_not_be_imported"
[project]
name = "safe"
[project.scripts]
unsafe = "code_that_must_not_be_imported:run"
"""

    record = parse_manifest("pyproject.toml", data, limit=10)

    assert record.name == "safe"
    assert record.dependencies == ()
    assert "code_that_must_not_be_imported" not in repr(record)


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("go.mod", b"module example.test/app\nreplace example.test/a => ../a"),
        ("go.mod", b"module example.test/app\nexclude example.test/a v1.0.0"),
        ("go.mod", b"module example.test/app\nretract v1.0.0"),
        ("go.mod", b'module "example.test/app"'),
        ("go.mod", b'module example.test/app\nrequire "example.test/a" v1.0.0'),
        (
            "pom.xml",
            b"<project><groupId>${project.groupId}</groupId><artifactId>app</artifactId></project>",
        ),
        (
            "pom.xml",
            b'<project xmlns="https://unsupported.test/format"><groupId>org.example</groupId><artifactId>app</artifactId></project>',
        ),
        (
            "pom.xml",
            b'<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:x="https://unsupported.test"><x:groupId>org.example</x:groupId><artifactId>app</artifactId></project>',
        ),
        (
            "pyproject.toml",
            b"""[project]\ndependencies = ["requests; (python_version >= '3.12')"]""",
        ),
        (
            "pyproject.toml",
            b"""[project]\ndependencies = ["requests; dependency_groups == 'test'"]""",
        ),
    ],
)
def test_unsupported_metadata_has_a_distinct_status(path: str, data: bytes) -> None:
    record = parse_manifest(path, data, limit=10)

    assert record.status is ManifestStatus.UNSUPPORTED
    assert record.name is None
    assert record.dependencies == ()
    assert record.source_sha256 == hashlib.sha256(data).hexdigest()


def test_python_marker_grammar_extracts_names_without_evaluating_conditions() -> None:
    data = b"""[project]
dependencies = [
 "one(>=1,<2); python_version >= '3.12' and sys_platform != 'win32'",
 "two; extra in 'test' or os_name not in 'nt'",
 "three; 'linux' == sys_platform",
]
"""

    record = parse_manifest("pyproject.toml", data, limit=10)

    assert record.status is ManifestStatus.PARSED
    assert record.dependencies == ("one", "three", "two")
