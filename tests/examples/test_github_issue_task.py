"""The Issue example declares its GitHub credential to every Git child process."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from examples.github import run_issue_task as script
from tests.support.git import git

from cairn.core.tasks import TaskSpec
from cairn.infra.git import WorktreeProvider
from cairn.infra.git.environment import build_git_env
from cairn.infra.github.models import IssueTask
from cairn.infra.tools.bash import build_command_env
from cairn.infra.workspace.workspace import Workspace

TOKEN_NAME = "DISPOSABLE_GH_TOKEN"


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Cairn Tests")
    git(root, "config", "user.email", "cairn-tests@example.invalid")
    (root / "answer.txt").write_text("wrong\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    git(root, "tag", "origin/main")
    return Workspace(root)


def _stub_network_and_model(
    monkeypatch: pytest.MonkeyPatch, observed: dict[str, frozenset[str]]
) -> None:
    """Stub every network and model seam; capture the worktree credential policy."""

    async def failing_create(
        self: WorktreeProvider, base_ref: str, branch: str | None
    ) -> Any:
        observed["secret_env_keys"] = self.secret_env_keys
        raise OSError("stop before Git work")

    async def issue(reference: Any, *, target_repository: Any, **_kwargs: Any) -> Any:
        return IssueTask(
            source=reference,
            source_url=f"https://github.com/{reference.repository.full_name}/issues/1",
            title="Change answer",
            task=TaskSpec(task_id="task", prompt="Make answer.txt correct"),
            base_branch="main",
        )

    monkeypatch.setattr(WorktreeProvider, "create", failing_create)
    monkeypatch.setattr(script, "GitHubRESTIssueReader", lambda _client: object())
    monkeypatch.setattr(
        script, "GitHubTaskSource", lambda _reader: SimpleNamespace(fetch=issue)
    )
    monkeypatch.setattr(
        script,
        "load_project_providers",
        lambda _path: SimpleNamespace(layout=None, providers=None),
    )
    monkeypatch.setattr(
        script,
        "default_provider",
        lambda *_args: SimpleNamespace(
            name="stub",
            config=SimpleNamespace(
                model_config=(SimpleNamespace(name="group"),),
                api_key_env="UNUSED_KEY",
            ),
        ),
    )
    monkeypatch.setattr(script, "confirm_provider_access", lambda *_a, **_k: True)
    monkeypatch.setattr(
        script, "resolve_provider_api_key", lambda *_a, **_k: "stub-credential"
    )
    monkeypatch.setattr(script, "resolve_context_budget", lambda *_a, **_k: None)
    monkeypatch.setattr(
        script, "build_provider_runtime", lambda *_a, **_k: SimpleNamespace()
    )
    monkeypatch.setattr(
        script,
        "SessionPermissionHandler",
        lambda _prompt: SimpleNamespace(
            authorize=lambda request: SimpleNamespace(
                allowed=True, granted_capabilities=frozenset({request.capability})
            )
        ),
    )


def _arguments(source: Workspace, tmp_path: Path) -> Any:
    expected = tmp_path / "expected.txt"
    expected.write_text("correct\n")
    return script.parser().parse_args(
        [
            "--disposable-repository",
            "--owner",
            "owner",
            "--repo",
            "repo",
            "--issue",
            "1",
            "--source",
            str(source.root),
            "--worktree-parent",
            str(tmp_path / "tasks"),
            "--base-ref",
            "origin/main",
            "--base-branch",
            "main",
            "--check-file",
            "answer.txt",
            "--expected-file",
            str(expected),
            "--token-env",
            TOKEN_NAME,
        ]
    )


def test_issue_run_declares_token_to_worktree_before_creation(
    source: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator owns Git child environments, so it needs the token name early.

    A LocalWorkflow-only declaration would be applied after the WorktreeProvider
    constructed its Git environment; the operator is the narrower trust boundary.
    """
    monkeypatch.delenv(TOKEN_NAME, raising=False)
    observed: dict[str, frozenset[str]] = {}
    _stub_network_and_model(monkeypatch, observed)
    with pytest.raises(OSError, match="stop before Git work"):
        asyncio.run(script.main(_arguments(source, tmp_path)))
    assert TOKEN_NAME in observed["secret_env_keys"]


def test_token_never_reaches_git_or_bash_child_processes() -> None:
    """Declaring the token strips it from Git children and from Agent Bash."""
    host = {"PATH": "/usr/bin", "HOME": "/home", TOKEN_NAME: "gh-secret-value"}
    git_env = build_git_env(host, frozenset({TOKEN_NAME}))
    assert TOKEN_NAME not in git_env
    assert "gh-secret-value" not in git_env.values()
    # Bash intentionally inherits the host environment, so an exported token is
    # only contained when the runtime declares it to the Agent's tool policy.
    assert TOKEN_NAME not in build_command_env(host, frozenset({TOKEN_NAME}))
    assert TOKEN_NAME in build_command_env(host)
