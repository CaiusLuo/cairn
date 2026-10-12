"""Keep documentation navigable and its source references true.

Documentation drifts quietly: a guide is moved, a module is renamed, and the
prose keeps pointing at where things used to be. These checks fail on the cheap,
mechanical part of that drift so review can focus on whether the prose is still
correct.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

MARKDOWN_SUFFIX = ".md"
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "tests" / "README.md",
    *sorted((ROOT / "docs").rglob(f"*{MARKDOWN_SUFFIX}")),
)

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_CODE_PATH = re.compile(r"`((?:src|tests|docs|examples)/[^`\s]+)`")
_PYTEST_PATH = re.compile(r"uv run pytest ([^\s`|]+)")

#: Prefixes that name a real repository path rather than illustrative prose.
_PATH_PREFIXES = ("src/", "tests/", "docs/", "examples/")


def _documents() -> list[Path]:
    return [path for path in DOCUMENTS if path.is_file()]


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


@pytest.mark.parametrize("document", _documents(), ids=lambda path: path.name)
def test_relative_links_resolve(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    broken: list[str] = []
    for match in _LINK.finditer(text):
        target = match.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path_part = target.split("#", 1)[0]
        if not path_part:
            continue
        if not (document.parent / path_part).exists():
            broken.append(f"line {_line_of(text, match.start())}: {target}")

    assert not broken, (
        f"{document.relative_to(ROOT)} has links that do not resolve:\n"
        + "\n".join(broken)
    )


@pytest.mark.parametrize("document", _documents(), ids=lambda path: path.name)
def test_referenced_paths_exist(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    missing: list[str] = []
    for match in _CODE_PATH.finditer(text):
        target = match.group(1).split("::", 1)[0].rstrip(".,;:")
        if not target.startswith(_PATH_PREFIXES):
            continue
        if "*" in target or "<" in target:
            continue
        if not (ROOT / target).exists():
            missing.append(f"line {_line_of(text, match.start())}: {target}")

    assert not missing, (
        f"{document.relative_to(ROOT)} references paths that do not exist:\n"
        + "\n".join(missing)
    )


@pytest.mark.parametrize("document", _documents(), ids=lambda path: path.name)
def test_referenced_pytest_paths_exist(document: Path) -> None:
    text = document.read_text(encoding="utf-8")
    missing: list[str] = []
    for match in _PYTEST_PATH.finditer(text):
        target = match.group(1).strip().rstrip("\\")
        if target.startswith("-") or not target.startswith(("tests", "src")):
            continue
        if not (ROOT / target.split("::", 1)[0]).exists():
            missing.append(f"line {_line_of(text, match.start())}: {target}")

    assert not missing, (
        f"{document.relative_to(ROOT)} documents test commands that do not exist:\n"
        + "\n".join(missing)
    )


def test_frozen_pilot_evidence_is_present() -> None:
    """The V1 pilot is historical evidence; its records must keep existing."""
    pilot = ROOT / "docs" / "evals" / "repository-intelligence-v1" / "pilot-20261011"
    assert (pilot / "experiment.json").is_file()
    assert (pilot / "comparison.json").is_file()
    assert (pilot / "findings.md").is_file()
    assert (pilot / "runs").is_dir()
    assert any((pilot / "runs").glob("*.json"))
