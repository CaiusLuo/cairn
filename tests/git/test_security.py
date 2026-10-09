import asyncio
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from cairn.git import WorktreeError, WorktreeHandle, WorktreeProvider
from cairn.git.environment import build_git_env
from cairn.workspace.workspace import Workspace
from tests.git.helpers import assert_removed, git

FAKE_SECRETS = {
    "CAIRN_LLM_API_KEY": "fake-cairn-credential",
    "ODD_SESSION": "fake-active-provider-credential",
    # A deliberately allowlisted name for an unselected provider.
    "LANG": "fake-unselected-provider-credential",
}


def configure_providers(source: Workspace) -> None:
    config = source.root / ".cairn" / "models.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("""[[providers]]
name = "active"
base_url = "https://example.invalid/v1"
api_key_env = "ODD_SESSION"
[[providers.models]]
name = "default"
model_ids = ["test/model"]
[[providers]]
name = "unselected"
base_url = "https://example.invalid/v1"
api_key_env = "LANG"
[[providers.models]]
name = "default"
model_ids = ["test/model"]
""")


def install_git_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    system_config: Path | None = None,
    fail_add: bool = False,
) -> Path:
    real_git = shutil.which("git")
    assert real_git is not None
    log = tmp_path / "git-environments.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    # Record the actual child environment and then run actual Git. No mocked
    # subprocess results: the failure mode performs a real add before failing.
    shim.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys
with open({str(log)!r}, "a") as output:
    output.write(json.dumps({{"args": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
if {system_config is not None!r}:
    os.environ["GIT_CONFIG_SYSTEM"] = {str(system_config)!r}
code = subprocess.call([{real_git!r}, *sys.argv[1:]])
if {fail_add!r} and "worktree" in sys.argv and "add" in sys.argv:
    sys.exit(42)
sys.exit(code)
""")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return log


def records(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def assert_no_secrets(text: str) -> None:
    for value in FAKE_SECRETS.values():
        assert value not in text


def test_environment_allowlist_and_credential_precedence() -> None:
    host = {
        **FAKE_SECRETS,
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp/home",
        "XDG_CONFIG_HOME": "/tmp/config",
        "GIT_DIR": "/unowned",
        "GIT_CONFIG_PARAMETERS": "malicious",
        "PYTHONPATH": "/unowned",
        "LD_PRELOAD": "unowned",
        "DYLD_INSERT_LIBRARIES": "unowned",
        "BASH_ENV": "/unowned",
    }
    before = dict(host)
    env = build_git_env(host, frozenset({"ODD_SESSION", "LANG", "HOME", "PATH"}))
    assert env == {"XDG_CONFIG_HOME": "/tmp/config", "GIT_TERMINAL_PROMPT": "0"}
    assert host == before
    assert "GIT_TERMINAL_PROMPT" not in build_git_env(
        host, frozenset({"GIT_TERMINAL_PROMPT"})
    )


@pytest.mark.parametrize("mode", ["release", "retain", "managed", "rollback"])
def test_real_git_environment_for_entire_lifecycle(
    source: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    configure_providers(source)
    with monkeypatch.context() as context:
        log = install_git_recorder(tmp_path, context, fail_add=mode == "rollback")
        for name, value in FAKE_SECRETS.items():
            context.setenv(name, value)
        context.setenv("XDG_CONFIG_HOME", "fake-programmatic-credential")
        context.setenv("GIT_DIR", str(tmp_path / "unowned"))
        context.setenv("GIT_WORK_TREE", str(tmp_path))
        context.setenv("GIT_INDEX_FILE", str(tmp_path / "unowned-index"))
        context.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "unowned-config"))
        context.setenv("GIT_CONFIG_COUNT", "1")
        context.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
        context.setenv("GIT_CONFIG_VALUE_0", "unowned")
        context.setenv("PYTHONPATH", str(tmp_path / "unowned-python"))
        before = dict(os.environ)
        provider = WorktreeProvider(
            source,
            tmp_path / "worktrees",
            secret_env_keys=frozenset({"XDG_CONFIG_HOME"}),
        )

        async def scenario() -> None:
            if mode == "rollback":
                with pytest.raises(WorktreeError, match="42") as error:
                    await provider.create("base", "task")
                assert_no_secrets(
                    str(error.value) + repr(getattr(error.value, "__notes__", ()))
                )
            elif mode == "managed":
                primary = ValueError("body failure")
                with pytest.raises(ValueError) as body_error:
                    async with provider.managed("base", "task") as handle:
                        (handle.path / "edit").write_text("temporary")
                        raise primary
                assert body_error.value is primary
                assert_removed_without_git(handle)
            else:
                handle = await provider.create("base", "task")
                if mode == "retain":
                    handle.retain()
                    assert handle.path.exists()
                await handle.release()
                assert_removed_without_git(handle)

        asyncio.run(scenario())
        assert dict(os.environ) == before
        observed = records(log)
        assert any("add" in item["args"] for item in observed)
        assert any("remove" in item["args"] for item in observed)
        assert any("-d" in item["args"] for item in observed)
        for item in observed:
            env = item["env"]
            assert (
                not (set(FAKE_SECRETS) | {"XDG_CONFIG_HOME", "PYTHONPATH"}) & env.keys()
            )
            assert not [
                key
                for key in env
                if key.startswith("GIT_") and key != "GIT_TERMINAL_PROMPT"
            ]
        assert_no_secrets(log.read_text())
        assert "fake-programmatic-credential" not in log.read_text()
    assert not list(provider.parent.iterdir())
    assert git(source.root, "branch", "--list", "task") == ""
    assert len(git(source.root, "worktree", "list").splitlines()) == 1
    assert not (tmp_path / "unowned-index").exists()
    output = capsys.readouterr()
    assert_no_secrets(output.out + output.err + caplog.text)
    assert not (source.root / ".cairn" / "traces").exists()


def assert_removed_without_git(handle: WorktreeHandle) -> None:
    assert not handle.path.exists()
    assert handle.state.value == "released"


@pytest.mark.parametrize("driver", ["clean", "smudge", "process"])
@pytest.mark.parametrize(
    "scope", ["local", "global", "xdg", "system", "include", "worktree"]
)
def test_external_filters_rejected_before_resources(
    source: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    driver: str,
    scope: str,
) -> None:
    marker = tmp_path / "filter-executed"
    script = tmp_path / "malicious-filter"
    script.write_text(
        f"#!/bin/sh\nprintf '%s' \"$CAIRN_LLM_API_KEY\" > {shlex.quote(str(marker))}\nexit 1\n"
    )
    script.chmod(0o755)
    (source.root / ".gitattributes").write_text("file.txt filter=malicious\n")
    git(source.root, "add", ".gitattributes")
    git(source.root, "commit", "-m", "filter attributes")
    # The command contains a fake value too, to ensure diagnostics do not echo it.
    command = shlex.quote(str(script)) + " " + FAKE_SECRETS["CAIRN_LLM_API_KEY"]
    config_text = f'[filter "malicious"]\n    {driver} = {command}\n'
    with monkeypatch.context() as context:
        if scope == "local":
            git(source.root, "config", f"filter.malicious.{driver}", command)
        elif scope == "global":
            (Path(os.environ["HOME"]) / ".gitconfig").write_text(config_text)
        elif scope == "xdg":
            config = Path(os.environ["XDG_CONFIG_HOME"]) / "git" / "config"
            config.parent.mkdir(parents=True)
            config.write_text(config_text)
        elif scope == "system":
            config = tmp_path / "system-config"
            config.write_text(config_text)
            install_git_recorder(tmp_path, context, system_config=config)
        elif scope == "include":
            config = tmp_path / "included-config"
            config.write_text(config_text)
            git(source.root, "config", "include.path", str(config))
        else:
            git(source.root, "config", "extensions.worktreeConfig", "true")
            (source.root / ".git" / "config.worktree").write_text(config_text)
        context.setenv("CAIRN_LLM_API_KEY", FAKE_SECRETS["CAIRN_LLM_API_KEY"])
        provider = WorktreeProvider(source, tmp_path / "worktrees")
        with pytest.raises(WorktreeError, match=r"V1 rejects.*filters") as error:
            asyncio.run(provider.create("HEAD", "task"))
        assert_no_secrets(
            str(error.value) + repr(getattr(error.value, "__notes__", ()))
        )
        assert not provider.parent.exists()
        assert not marker.exists()
    assert git(source.root, "branch", "--list", "task") == ""
    assert len(git(source.root, "worktree", "list").splitlines()) == 1
    assert not (source.root / ".git" / "worktrees").exists()
    output = capsys.readouterr()
    assert_no_secrets(output.out + output.err + caplog.text)
    assert not (source.root / ".cairn" / "traces").exists()


def test_unused_filter_configuration_is_still_rejected(
    provider: WorktreeProvider,
    source: Workspace,
) -> None:
    git(source.root, "config", "filter.unused.smudge", "exit 1")
    with pytest.raises(WorktreeError, match="V1 rejects"):
        asyncio.run(provider.create("base", None))
    assert not provider.parent.exists()


def test_hooks_and_fsmonitor_do_not_execute(
    provider: WorktreeProvider,
    source: Workspace,
    tmp_path: Path,
) -> None:
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook_marker = tmp_path / "hook-executed"
    monitor_marker = tmp_path / "monitor-executed"
    for name in ("post-checkout", "reference-transaction"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(hook_marker))}\n")
        hook.chmod(0o755)
    monitor = tmp_path / "monitor"
    monitor.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(monitor_marker))}\nexit 1\n")
    monitor.chmod(0o755)
    git(source.root, "config", "core.hooksPath", str(hooks))
    git(source.root, "config", "core.fsmonitor", str(monitor))

    async def scenario() -> None:
        handle = await provider.create("base", "task")
        handle.retain()
        await handle.release()
        assert_removed(handle, source)
        async with provider.managed("HEAD", None) as ephemeral:
            (ephemeral.path / "edit").write_text("temporary")
        assert_removed(ephemeral, source)

    asyncio.run(scenario())
    assert not hook_marker.exists()
    assert not monitor_marker.exists()
    # Positive controls: these scripts execute under ordinary Git without the
    # lifecycle command overrides, so absence above proves actual suppression.
    git(source.root, "status", "--porcelain")
    assert monitor_marker.exists()
    git(source.root, "branch", "hook-control", "base")
    assert hook_marker.exists()


def test_filter_added_after_creation_refuses_default_release_but_allows_discard(
    provider: WorktreeProvider,
    source: Workspace,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "filter-executed"
    script = tmp_path / "filter"
    script.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\ncat\n")
    script.chmod(0o755)

    async def scenario() -> None:
        handle = await provider.create("base", "task")
        (handle.path / ".gitattributes").write_text("file.txt filter=malicious\n")
        git(source.root, "config", "filter.malicious.clean", shlex.quote(str(script)))
        with pytest.raises(WorktreeError, match="V1 rejects"):
            await handle.release()
        assert handle.path.exists()
        assert not marker.exists()
        await handle.release(discard_changes=True)
        assert_removed(handle, source)
        assert not marker.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("condition", ["branch", "gitdir"])
@pytest.mark.parametrize("driver", ["smudge", "process"])
def test_target_conditional_filters_rejected_before_checkout(
    provider: WorktreeProvider,
    source: Workspace,
    tmp_path: Path,
    driver: str,
    condition: str,
) -> None:
    marker = tmp_path / "filter-executed"
    script = tmp_path / "filter"
    script.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
    script.chmod(0o755)
    (source.root / ".gitattributes").write_text("file.txt filter=malicious\n")
    git(source.root, "add", ".gitattributes")
    git(source.root, "commit", "-m", "filter attributes")
    config = tmp_path / "conditional-config"
    config.write_text(f'[filter "malicious"]\n{driver} = {shlex.quote(str(script))}\n')
    key = (
        "onbranch:task"
        if condition == "branch"
        else f"gitdir:{source.root}/.git/worktrees/**"
    )
    git(source.root, "config", f"includeIf.{key}.path", str(config))
    # The source context does not activate this configuration.
    assert git(source.root, "config", "--get", "includeIf." + key + ".path") == str(
        config
    )
    with pytest.raises(WorktreeError, match="V1 rejects") as error:
        asyncio.run(provider.create("HEAD", "task"))
    assert not getattr(error.value, "__notes__", ())
    assert not marker.exists()
    assert not list(provider.parent.iterdir())
    assert git(source.root, "branch", "--list", "task") == ""
    assert len(git(source.root, "worktree", "list").splitlines()) == 1
    assert not list((source.root / ".git" / "worktrees").glob("*"))


def test_empty_commit_tree_can_be_created_and_released(
    provider: WorktreeProvider,
    source: Workspace,
) -> None:
    git(source.root, "rm", "file.txt", ".gitignore")
    git(source.root, "commit", "-m", "empty tree")

    async def scenario() -> None:
        handle = await provider.create("HEAD", None)
        assert [p.name for p in handle.path.iterdir()] == [".git"]
        await handle.release()
        assert_removed(handle, source)

    asyncio.run(scenario())


def test_legacy_provider_credential_can_override_allowlist(
    source: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = source.root / ".cairn" / "models.toml"
    config.parent.mkdir()
    config.write_text("""base_url = "https://example.invalid/v1"
api_key_env = "LANG"
[[models]]
name = "default"
model_ids = ["test/model"]
""")
    log = install_git_recorder(tmp_path, monkeypatch)
    monkeypatch.setenv("LANG", FAKE_SECRETS["LANG"])
    provider = WorktreeProvider(source, tmp_path / "worktrees")

    async def scenario() -> None:
        handle = await provider.create("base", None)
        await handle.release()

    asyncio.run(scenario())
    assert all("LANG" not in item["env"] for item in records(log))
    assert_no_secrets(log.read_text())


def test_invalid_provider_config_fails_closed_without_git_or_resources(
    provider: WorktreeProvider,
    source: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = source.root / ".cairn" / "models.toml"
    config.parent.mkdir()
    config.write_text("invalid TOML " + FAKE_SECRETS["CAIRN_LLM_API_KEY"])
    log = install_git_recorder(tmp_path, monkeypatch)
    with pytest.raises(WorktreeError, match="credential names") as error:
        asyncio.run(provider.create("base", "task"))
    assert_no_secrets(str(error.value))
    assert not log.exists()
    assert not provider.parent.exists()
