"""CI must never mistake changed executable inputs for documentation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.git import git, initialize_repository

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci/scope.py"


def run_scope(root: Path, base: str, *, event: str = "push") -> str:
    output = root / "ci-output"
    subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=root,
        env={
            **os.environ,
            "CI_EVENT": event,
            "CI_BASE": base,
            "CI_HEAD": git(root, "rev-parse", "HEAD"),
            "GITHUB_OUTPUT": str(output),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    return output.read_text().strip()


@pytest.mark.parametrize(
    ("path", "docs_only"),
    [
        ("README.md", True),
        ("docs/guides/testing.md", True),
        ("tests/README.md", True),
        ("scripts/README.md", True),
        ("src/new.py", False),
        ("tests/new.py", False),
        ("tests/fixtures/prompt.md", False),
        ("docs/example.py", False),
        ("skills/example/SKILL.md", False),
        ("web/new.ts", False),
        ("uv.lock", False),
        (".github/workflows/ci.yml", False),
        ("unknown", False),
    ],
)
def test_new_paths_are_selected_conservatively(
    tmp_path: Path, path: str, docs_only: bool
) -> None:
    initialize_repository(tmp_path)
    base = git(tmp_path, "rev-parse", "HEAD")
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("new\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "add file")
    assert run_scope(tmp_path, base) == f"docs_only={str(docs_only).lower()}"


@pytest.mark.parametrize("rename", [False, True])
def test_removing_code_with_documentation_requires_acceptance(
    tmp_path: Path, rename: bool
) -> None:
    source = initialize_repository(tmp_path, filename="module.py")
    base = git(tmp_path, "rev-parse", "HEAD")
    if rename:
        source.rename(tmp_path / "README.md")
    else:
        source.unlink()
        (tmp_path / "README.md").write_text("removed code\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "remove code")
    assert run_scope(tmp_path, base) == "docs_only=false"


@pytest.mark.parametrize("with_code", [False, True])
def test_pr_diff_uses_merge_base_and_keeps_earlier_code_changes(
    tmp_path: Path, with_code: bool
) -> None:
    initialize_repository(tmp_path)
    git(tmp_path, "checkout", "-b", "feature")
    if with_code:
        (tmp_path / "new.py").write_text("pass\n")
        git(tmp_path, "add", ".")
        git(tmp_path, "commit", "-m", "code")
    (tmp_path / "README.md").write_text("later docs\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "docs")
    feature = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "main")
    (tmp_path / "main.py").write_text("pass\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "main advances")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", feature)
    assert run_scope(tmp_path, base, event="pull_request") == (
        f"docs_only={str(not with_code).lower()}"
    )


@pytest.mark.parametrize("base", ["HEAD", "missing-ref", "0" * 40, ""])
def test_empty_or_unavailable_diff_runs_full_acceptance(
    tmp_path: Path, base: str
) -> None:
    initialize_repository(tmp_path)
    assert run_scope(tmp_path, base) == "docs_only=false"


def test_manual_run_forces_full_acceptance_even_for_docs(tmp_path: Path) -> None:
    initialize_repository(tmp_path)
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("docs\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "docs")
    assert run_scope(tmp_path, base, event="workflow_dispatch") == "docs_only=false"
