import asyncio
from pathlib import Path
from typing import Any

import pytest
from examples.github import run_issue_task as example

from cairn.core.permissions import PermissionChoice, SessionPermissionHandler


def arguments(tmp_path: Path, *extra: str) -> Any:
    source = tmp_path / "source"
    source.mkdir()
    return example.parser().parse_args(
        [
            "--owner",
            "owner",
            "--repo",
            "project",
            "--issue",
            "20",
            "--source",
            str(source),
            "--worktree-parent",
            str(tmp_path / "worktrees"),
            "--base-ref",
            "HEAD",
            "--base-branch",
            "main",
            "--check-file",
            "answer.txt",
            "--expected-file",
            str(tmp_path / "expected.txt"),
            *extra,
        ]
    )


def reject_transport(**kwargs: Any) -> Any:
    pytest.fail("Transport must not be constructed before explicit authorization")


def test_manual_example_requires_opt_in_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(example, "GitHubREST", reject_transport)
    with pytest.raises(ValueError, match="Opt in"):
        asyncio.run(example.main(arguments(tmp_path)))
    assert not (tmp_path / "worktrees").exists()


def test_manual_read_denial_does_not_construct_transport_or_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(example, "GitHubREST", reject_transport)
    monkeypatch.setattr(
        example,
        "SessionPermissionHandler",
        lambda prompt: SessionPermissionHandler(lambda request: PermissionChoice.DENY),
    )
    assert (
        asyncio.run(example.main(arguments(tmp_path, "--disposable-repository"))) == 1
    )
    assert not (tmp_path / "worktrees").exists()


def test_manual_example_rejects_invalid_credential_variable_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(example, "GitHubREST", reject_transport)
    with pytest.raises(ValueError, match="credential environment variable"):
        asyncio.run(
            example.main(
                arguments(
                    tmp_path, "--disposable-repository", "--token-env", "INVALID\nENV"
                )
            )
        )
