"""Real offline project-shell boundary checks against disposable sentinels."""

import asyncio
import shlex
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from cairn.infra import sandbox
from cairn.infra.tools.bash import BashTool
from cairn.infra.workspace.workspace import Workspace


def _tool(root: Path) -> BashTool:
    if not sandbox.available():
        pytest.skip("bubblewrap sandbox is unavailable")
    return BashTool(Workspace(root), sandbox_shell=True)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout.strip()


def test_shell_masks_credentials_and_hides_other_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "other-project.txt"
    outside.write_text("OTHER_PROJECT_SENTINEL")
    names = (
        ".env",
        ".env.staging",
        "settings/.env",
        "key.pem",
        "private.key",
        ".aws/credentials",
        ".ssh/id_ed25519",
        ".docker/config.json",
    )
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("CREDENTIAL_SENTINEL")
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "ENVIRONMENT_SENTINEL")
    tool = _tool(root)
    command = "\n".join(
        [f"cat {shlex.quote(name)} 2>/dev/null || true" for name in names]
        + [
            f"cat {shlex.quote(str(outside))} 2>/dev/null || true",
            "printf '%s' \"${UNRELATED_HOST_SECRET-}\"",
            "python -c \"from pathlib import Path; print(Path('settings/.env').read_text())\"",
            "printf 'ordinary' > ordinary.txt",
        ]
    )
    result = asyncio.run(tool.execute({"command": command}))
    assert result.exit_code == 0, result.stderr
    assert "SENTINEL" not in result.stdout + result.stderr
    assert (root / "ordinary.txt").read_text() == "ordinary"
    for name in names:
        assert (root / name).read_text() == "CREDENTIAL_SENTINEL"
    assert outside.read_text() == "OTHER_PROJECT_SENTINEL"


def test_shell_cannot_undo_masks_remount_or_create_user_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("CREDENTIAL_SENTINEL")
    system = tmp_path / "mock-system"
    system.mkdir()
    protected = system / "protected.txt"
    protected.write_text("HOST_SYSTEM_SENTINEL")
    original = sandbox.build_shell_argv

    def argv(
        workspace: Workspace,
        command: str,
        *,
        empty_file_fd: Callable[[], int],
        private_paths: Sequence[Path] | None = None,
    ) -> list[str]:
        built = original(
            workspace, command, empty_file_fd=empty_file_fd, private_paths=private_paths
        )
        # The escape attempt can only touch this disposable mock system mount.
        built[1:1] = ["--ro-bind", str(system), str(system)]
        return built

    monkeypatch.setattr(sandbox, "build_shell_argv", argv)
    tool = _tool(root)
    command = (
        "umount .env 2>/dev/null; echo unmask=$?; "
        f"mount -o remount,rw,bind {shlex.quote(str(system))} 2>/dev/null; "
        "echo remount=$?; "
        f"printf hacked > {shlex.quote(str(protected))} 2>/dev/null; "
        "echo write=$?; "
        "unshare -Ur true 2>/dev/null; echo userns=$?; "
        "cat .env; grep '^CapEff:' /proc/self/status"
    )
    result = asyncio.run(tool.execute({"command": command}))
    assert result.exit_code == 0, result.stderr
    for operation in ("unmask", "remount", "write", "userns"):
        assert f"{operation}=0" not in result.stdout
        assert f"{operation}=" in result.stdout
    assert "CREDENTIAL_SENTINEL" not in result.stdout + result.stderr
    assert "CapEff:\t0000000000000000" in result.stdout
    assert protected.read_text() == "HOST_SYSTEM_SENTINEL"
    assert (root / ".env").read_text() == "CREDENTIAL_SENTINEL"


def test_linked_worktree_shell_supports_python_uv_and_withholds_git(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    (source / "initial.txt").write_text("initial\n")
    (source / ".env").write_text("COMMITTED_CREDENTIAL_SENTINEL")
    _git(source, "add", "initial.txt", ".env")
    _git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=f@invalid",
        "commit",
        "-m",
        "initial",
    )
    original_head = _git(source, "rev-parse", "HEAD")
    original_config = (source / ".git/config").read_bytes()
    root = tmp_path / "project"
    _git(source, "worktree", "add", "-b", "fixture-project", str(root))
    subprocess.run(
        [
            str(getattr(sys, "_base_executable", sys.executable)),
            "-m",
            "venv",
            "--without-pip",
            str(root / ".venv"),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    wheel = root / "fixture_demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("fixture_demo.py", "VALUE = 'installed-offline'\n")
        archive.writestr(
            "fixture_demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: fixture-demo\nVersion: 1.0\n",
        )
        archive.writestr(
            "fixture_demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr("fixture_demo-1.0.dist-info/RECORD", "")
    tool = _tool(root)
    result = asyncio.run(
        tool.execute(
            {
                "command": "set -e; python --version; uv --version; "
                "uv pip install --python .venv/bin/python --no-index ./fixture_demo-1.0-py3-none-any.whl; "
                "python -c 'import fixture_demo; print(fixture_demo.VALUE)'; "
                "printf 'change\\n' > changed.txt; "
                "if git show HEAD:.env; then exit 77; fi; "
                "cat .git; test -f changed.txt"
            }
        )
    )
    assert result.exit_code == 0, result.stderr
    assert "installed-offline" in result.stdout
    assert "COMMITTED_CREDENTIAL_SENTINEL" not in result.stdout + result.stderr
    assert _git(source, "rev-parse", "HEAD") == original_head
    assert (source / ".git/config").read_bytes() == original_config
    assert _git(source, "status", "--porcelain") == ""
    assert _git(root, "rev-parse", "HEAD") == original_head
    assert _git(root, "show", "HEAD:.env") == "COMMITTED_CREDENTIAL_SENTINEL"
    assert (root / "changed.txt").read_text() == "change\n"


def test_shell_does_not_follow_unregistered_git_pointer(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "other.git"
    outside.mkdir()
    (outside / "secret").write_text("OTHER_PROJECT_SENTINEL")
    (root / ".git").write_text(f"gitdir: {outside}\n")
    tool = _tool(root)
    result = asyncio.run(
        tool.execute({"command": f"cat .git; cat {outside}/secret 2>/dev/null || true"})
    )
    assert result.exit_code == 0, result.stderr
    assert "gitdir" not in result.stdout
    assert "SENTINEL" not in result.stdout + result.stderr


@pytest.mark.parametrize("nested", [False, True])
def test_shell_withholds_git_objects_and_auth_configuration(
    tmp_path: Path, nested: bool
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    repository = root / "nested" if nested else root
    repository.mkdir(exist_ok=True)
    _git(repository, "init")
    _git(repository, "config", "http.extraHeader", "AUTHORIZATION_SENTINEL")
    (repository / ".env").write_text("COMMITTED_CREDENTIAL_SENTINEL")
    _git(repository, "add", ".env")
    _git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=f@invalid",
        "commit",
        "-m",
        "fixture",
    )
    original_head = _git(repository, "rev-parse", "HEAD")
    tool = _tool(root)
    git_path = "nested/.git" if nested else ".git"
    result = asyncio.run(
        tool.execute(
            {
                "command": f"cat {git_path}/config 2>/dev/null || true; "
                f"git --git-dir={git_path} show HEAD:.env; echo git_exit=$?; "
                f"ls -A {git_path}; test ! -e {git_path}/objects"
            }
        )
    )
    assert result.exit_code == 0, result.stderr
    assert "git_exit=0" not in result.stdout
    assert "AUTHORIZATION_SENTINEL" not in result.stdout + result.stderr
    assert "COMMITTED_CREDENTIAL_SENTINEL" not in result.stdout + result.stderr
    assert _git(repository, "config", "http.extraHeader") == "AUTHORIZATION_SENTINEL"
    assert _git(repository, "rev-parse", "HEAD") == original_head
    assert _git(repository, "show", "HEAD:.env") == "COMMITTED_CREDENTIAL_SENTINEL"
    assert "Git operations" in tool.schema()["function"]["description"]


def test_shell_rejects_symlinked_git_entry(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").symlink_to(tmp_path, target_is_directory=True)
    result = asyncio.run(_tool(root).execute({"command": "touch started"}))
    assert result.error_type == "sandbox_unavailable"
    assert not (root / "started").exists()


@pytest.mark.parametrize("root", ["/", "/usr", "/usr/bin", "/etc", "/proc", "/tmp"])
def test_shell_rejects_protected_workspace(
    root: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not Path(root).is_dir():
        pytest.skip("protected root does not exist on this platform")
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/host-owned/bwrap")
    with pytest.raises(sandbox.IsolationUnavailable, match="protected root"):
        sandbox.build_shell_argv(
            Workspace(Path(root)), "true", empty_file_fd=lambda: -1
        )
