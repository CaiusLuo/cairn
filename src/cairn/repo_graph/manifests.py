"""Bounded manifest metadata; no configuration or build code is executed."""

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import cast
from xml.etree import ElementTree

from cairn.repo_graph.models import ManifestKind, ManifestRecord, ManifestStatus

MAX_NAME_LENGTH = 200
_MAX_REQUIREMENT_LENGTH = 4096
_MAX_XML_DEPTH = 64
_KINDS = {
    "pyproject.toml": ManifestKind.PYTHON,
    "package.json": ManifestKind.NODE,
    "pom.xml": ManifestKind.JAVA,
    "build.gradle": ManifestKind.GRADLE,
    "build.gradle.kts": ManifestKind.GRADLE,
    "go.mod": ManifestKind.GO,
    "Cargo.toml": ManifestKind.RUST,
}
_DISTRIBUTION = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_VERSION_SPECIFIERS = (
    r"(?:===|~=|==|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+"
    r"(?:\s*,\s*(?:===|~=|==|!=|<=|>=|<|>)\s*[A-Za-z0-9.*+!_-]+)*"
)
_REQUIREMENT = re.compile(
    rf"(?P<name>{_DISTRIBUTION})"
    rf"(?:\s*\[\s*{_DISTRIBUTION}(?:\s*,\s*{_DISTRIBUTION})*\s*\])?"
    rf"\s*(?:{_VERSION_SPECIFIERS}|\(\s*{_VERSION_SPECIFIERS}\s*\)"
    r"|@\s*[^\s;]+)?\s*"
)
_MARKER_VARIABLE = (
    r"(?:python_version|python_full_version|os_name|sys_platform|platform_release|"
    r"platform_system|platform_version|platform_machine|"
    r"platform_python_implementation|implementation_name|implementation_version|extra)"
)
_MARKER_OPERAND = rf"""(?:{_MARKER_VARIABLE}|"[^"\\\r\n]*"|'[^'\\\r\n]*')"""
_MARKER_COMPARISON = (
    rf"{_MARKER_OPERAND}\s*(?:===|~=|==|!=|<=|>=|<|>|\bnot\s+in\b|\bin\b)"
    rf"\s*{_MARKER_OPERAND}"
)
_MARKER = re.compile(
    rf"\s*{_MARKER_COMPARISON}(?:\s+(?:and|or)\s+{_MARKER_COMPARISON})*\s*"
)
_SAFE_NAMES = {
    ManifestKind.PYTHON: re.compile(_DISTRIBUTION),
    ManifestKind.NODE: re.compile(
        r"(?:@[A-Za-z0-9][A-Za-z0-9._-]*/)?[A-Za-z0-9][A-Za-z0-9._-]*"
    ),
    ManifestKind.JAVA: re.compile(
        r"[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9][A-Za-z0-9._-]*"
    ),
    ManifestKind.GO: re.compile(r"[A-Za-z0-9][A-Za-z0-9._~/-]*"),
    ManifestKind.RUST: re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*"),
}
_GO_VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?")


class _Malformed(ValueError):
    """A typed failure without source text or parser exception details."""


class _Unsupported(ValueError):
    """Metadata requires grammar or resolution outside this bounded parser."""


def _invalid_json_constant(value: str) -> object:
    raise _Malformed


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _Malformed
    return cast(dict[str, object], value)


@dataclass
class _Facts:
    kind: ManifestKind
    limit: int
    name: str | None = None
    dependencies: set[str] = field(default_factory=set)
    limited: bool = False

    def safe_name(self, value: object) -> str | None:
        if not isinstance(value, str):
            raise _Malformed
        if len(value) > MAX_NAME_LENGTH:
            self.limited = True
            return None
        if self.kind is ManifestKind.JAVA and "${" in value:
            raise _Unsupported
        if _SAFE_NAMES[self.kind].fullmatch(value) is None:
            raise _Malformed
        if self.kind is ManifestKind.GO and any(
            part in {"", ".", ".."} for part in value.split("/")
        ):
            raise _Malformed
        if self.kind is ManifestKind.PYTHON:
            return re.sub(r"[-_.]+", "-", value).lower()
        return value

    def dependency(self, value: object) -> None:
        name = self.safe_name(value)
        if name is None:
            return
        self.dependencies.add(name)
        if len(self.dependencies) > self.limit:
            self.dependencies.remove(max(self.dependencies))
            self.limited = True


def _python(facts: _Facts, document: object) -> None:
    root = _mapping(document)
    if "project" not in root:
        return
    project = _mapping(root["project"])
    if "name" in project:
        facts.name = facts.safe_name(project["name"])
    groups = [project.get("dependencies", [])]
    if "optional-dependencies" in project:
        groups.extend(_mapping(project["optional-dependencies"]).values())
    for dependencies in groups:
        if not isinstance(dependencies, list):
            raise _Malformed
        for requirement in dependencies:
            if not isinstance(requirement, str):
                raise _Malformed
            if len(requirement) > _MAX_REQUIREMENT_LENGTH:
                facts.limited = True
                continue
            if any(ord(character) < 32 for character in requirement):
                raise _Malformed
            requirement, separator, marker = requirement.partition(";")
            if separator:
                if not marker.strip():
                    raise _Malformed
                if _MARKER.fullmatch(marker) is None:
                    raise _Unsupported
            if "@" not in requirement and requirement.count("(") != requirement.count(
                ")"
            ):
                raise _Malformed
            match = _REQUIREMENT.fullmatch(requirement)
            if match is None:
                prefix = re.match(_DISTRIBUTION, requirement)
                if prefix is not None and requirement[
                    prefix.end() :
                ].lstrip().startswith(("[", "(", "<", ">", "=", "!", "~", "@")):
                    raise _Unsupported
                raise _Malformed
            facts.dependency(match.group("name"))


def _node(facts: _Facts, document: object) -> None:
    root = _mapping(document)
    if "name" in root:
        facts.name = facts.safe_name(root["name"])
    for key in (
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ):
        if key not in root:
            continue
        for name, version in _mapping(root[key]).items():
            if not isinstance(version, str):
                raise _Malformed
            facts.dependency(name)


def _cargo(facts: _Facts, document: object) -> None:
    root = _mapping(document)
    if "package" in root:
        package = _mapping(root["package"])
        if "name" in package:
            facts.name = facts.safe_name(package["name"])
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        if key not in root:
            continue
        for name, declaration in _mapping(root[key]).items():
            if not isinstance(declaration, (str, dict)):
                raise _Malformed
            facts.dependency(name)


def _xml_child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    namespace = element.tag.rpartition("}")[0] + "}" if "}" in element.tag else ""
    if any(
        child.tag.rsplit("}", 1)[-1] == name and child.tag != namespace + name
        for child in element
    ):
        raise _Unsupported
    matches = [child for child in element if child.tag == namespace + name]
    if len(matches) > 1:
        raise _Malformed
    return matches[0] if matches else None


def _xml_text(element: ElementTree.Element, name: str) -> str | None:
    child = _xml_child(element, name)
    if child is None:
        return None
    if len(child):
        raise _Malformed
    return (child.text or "").strip()


def _maven(facts: _Facts, data: bytes) -> None:
    # Decoding first also detects declarations hidden in UTF-16/32 byte encodings.
    document = data.decode("utf-8")
    if "<!DOCTYPE" in document.upper() or "<!ENTITY" in document.upper():
        raise _Malformed
    root = ElementTree.fromstring(document)
    if root.tag.rsplit("}", 1)[-1] != "project":
        raise _Malformed
    if root.tag not in {"project", "{http://maven.apache.org/POM/4.0.0}project"}:
        raise _Unsupported
    stack = [(root, 0)]
    while stack:
        element, depth = stack.pop()
        if depth > _MAX_XML_DEPTH:
            facts.limited = True
            return
        stack.extend((child, depth + 1) for child in element)
    group = _xml_text(root, "groupId")
    artifact = _xml_text(root, "artifactId")
    if group is None:
        parent = _xml_child(root, "parent")
        if parent is not None:
            group = _xml_text(parent, "groupId")
    if group is not None and artifact is not None:
        facts.name = facts.safe_name(f"{group}:{artifact}")
    elif group is not None or artifact is not None:
        raise _Malformed
    dependencies = _xml_child(root, "dependencies")
    if dependencies is None:
        return
    for dependency in dependencies:
        namespace = root.tag.rpartition("}")[0] + "}" if "}" in root.tag else ""
        if dependency.tag != namespace + "dependency":
            raise _Malformed
        group = _xml_text(dependency, "groupId")
        artifact = _xml_text(dependency, "artifactId")
        if group is None or artifact is None:
            raise _Malformed
        facts.dependency(f"{group}:{artifact}")


def _go_dependency(facts: _Facts, fields: list[str]) -> None:
    if any('"' in field or "`" in field for field in fields):
        raise _Unsupported
    if len(fields) != 2 or _GO_VERSION.fullmatch(fields[1]) is None:
        raise _Malformed
    facts.dependency(fields[0])


def _go(facts: _Facts, data: bytes) -> None:
    in_require = False
    has_module = False
    for raw_line in data.decode("utf-8").splitlines():
        line = raw_line.partition("//")[0].strip()
        if not line:
            continue
        fields = line.split()
        if in_require:
            if fields == [")"]:
                in_require = False
            else:
                _go_dependency(facts, fields)
        elif fields[0] == "module":
            if any('"' in field or "`" in field for field in fields[1:]):
                raise _Unsupported
            if has_module or len(fields) != 2:
                raise _Malformed
            facts.name = facts.safe_name(fields[1])
            has_module = True
        elif fields == ["require", "("]:
            in_require = True
        elif fields[0] == "require":
            _go_dependency(facts, fields[1:])
        elif fields[0] == "go":
            if (
                len(fields) != 2
                or re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", fields[1]) is None
            ):
                raise _Malformed
        elif fields[0] == "toolchain":
            if (
                len(fields) != 2
                or re.fullmatch(r"(?:default|go[0-9]+\.[0-9]+(?:\.[0-9]+)?)", fields[1])
                is None
            ):
                raise _Malformed
        else:
            # Quoted names and other directives need a fuller Go grammar.
            raise _Unsupported
    if in_require or not has_module:
        raise _Malformed


def parse_manifest(path: str, data: bytes, *, limit: int) -> ManifestRecord:
    """Extract safe names, keeping the first ``limit`` dependencies in sorted order.

    Unsupported syntax and invalid metadata have explicit, distinct statuses.
    Oversized names are omitted rather than changing their identity.
    The caller bounds file bytes before invoking this parser.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("Manifest item limit must be a positive integer")
    kind = _KINDS.get(PurePosixPath(path).name)
    if kind is None:
        raise ValueError("Unsupported manifest basename")
    source_sha256 = hashlib.sha256(data).hexdigest()
    if kind is ManifestKind.GRADLE:
        return ManifestRecord(
            path, kind, ManifestStatus.PRESENCE_ONLY, source_sha256, None, ()
        )
    facts = _Facts(kind, limit)
    try:
        if kind is ManifestKind.PYTHON:
            _python(facts, tomllib.loads(data.decode("utf-8")))
        elif kind is ManifestKind.NODE:
            document: object = json.loads(data, parse_constant=_invalid_json_constant)
            _node(facts, document)
        elif kind is ManifestKind.JAVA:
            _maven(facts, data)
        elif kind is ManifestKind.GO:
            _go(facts, data)
        elif kind is ManifestKind.RUST:
            _cargo(facts, tomllib.loads(data.decode("utf-8")))
    except _Unsupported:
        return ManifestRecord(
            path,
            kind,
            ManifestStatus.UNSUPPORTED,
            source_sha256,
            None,
            (),
            facts.limited,
        )
    except (
        _Malformed,
        UnicodeError,
        tomllib.TOMLDecodeError,
        json.JSONDecodeError,
        ElementTree.ParseError,
        RecursionError,
        ValueError,
    ):
        return ManifestRecord(
            path, kind, ManifestStatus.MALFORMED, source_sha256, None, (), facts.limited
        )
    status = ManifestStatus.LIMITED if facts.limited else ManifestStatus.PARSED
    return ManifestRecord(
        path,
        kind,
        status,
        source_sha256,
        facts.name,
        tuple(sorted(facts.dependencies)),
        facts.limited,
    )
