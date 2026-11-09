from pathlib import Path

import pytest

from cairn.server.config import load_server_config
from cairn.server.paths import ServerPaths


def test_precedence_paths_and_budget_do_not_depend_on_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[server]\nworkspace = "source"\ndata_root = "state"\nport = 9000\n[budget]\nmax_steps = 15\n[services]\nschedule = false\n'
    )
    (tmp_path / "source").mkdir()
    monkeypatch.setenv("CAIRN_PORT", "9001")
    monkeypatch.setenv("CAIRN_MAX_STEPS", "16")
    monkeypatch.chdir(tmp_path / "source")
    first = load_server_config(config, overrides={"port": 9002, "max_steps": 17})
    monkeypatch.chdir(tmp_path.parent)
    assert first == load_server_config(
        config, overrides={"port": 9002, "max_steps": 17}
    )
    assert first.port == 9002 and first.budget.max_steps == 17
    assert first.data_root == tmp_path / "state"
    assert first.worktrees_root == tmp_path / "state/worktrees"
    assert not first.services.schedule
    assert first.paths.sessions == tmp_path / "state/runtime/sessions"
    assert not first.data_root.exists()


@pytest.mark.parametrize(
    "text",
    [
        "[server]\nport = true",
        '[server]\noffline = "true"',
        '[server]\napi_key = "never-store-keys"',
        '[logging]\nlevel = "invalid"',
        '[services]\nwechat = "yes"',
        "[budget]\nmax_steps = 0",
        '[server]\nhost = "0.0.0.0"',
        '[secrets]\nkey = "private"',
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_server_config(path)


def test_env_data_root_wins_over_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[server]\ndata_root = "old"')
    monkeypatch.setenv("CAIRN_DATA_ROOT", str(tmp_path / "new"))
    config = load_server_config(path)
    assert config.data_root == tmp_path / "new"
    assert config.worktrees_root == tmp_path / "new/worktrees"
    monkeypatch.setenv("CAIRN_DATA_ROOT", "relative")
    with pytest.raises(ValueError, match="absolute"):
        load_server_config(path)


def test_explicit_missing_file_never_falls_back(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_server_config(tmp_path / "missing.toml")


@pytest.mark.parametrize("location", ["workspace", "worktrees", "symlink"])
def test_host_state_cannot_overlap_writable_roots(
    tmp_path: Path, location: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    data = source / "state" if location == "workspace" else tmp_path / "state"
    trees = data / "runtime/tasks" if location == "worktrees" else data / "worktrees"
    if location == "symlink":
        data.mkdir()
        (data / "runtime").symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError):
        ServerPaths(source, data, trees)


def test_explicit_data_root_does_not_evaluate_unused_xdg_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "invalid-relative-unused-default")
    config = load_server_config(overrides={"data_root": tmp_path / "selected"})
    assert config.data_root == tmp_path / "selected"


def test_host_policy_symlink_is_rejected_even_when_target_is_outside(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "provider.json"
    target.write_text("{}")
    link = source / "provider-link.json"
    link.symlink_to(target)
    config = load_server_config(
        overrides={"workspace": source, "provider_approval": link}
    )
    assert config.provider_approval is not None
    with pytest.raises(ValueError, match="Host configuration"):
        config.paths.protect_policy(config.provider_approval)
