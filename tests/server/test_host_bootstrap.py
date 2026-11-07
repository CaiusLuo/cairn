"""First-use publication stays isolated from operator config and state."""

import json
import os
import pwd
from pathlib import Path

import pytest

from cairn.infra import server_bootstrap as bootstrap
from cairn.infra.config.server import load_server_config


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    root = tmp_path / "source"
    (root / "deploy/systemd").mkdir(parents=True)
    (root / "deploy/config.example.toml").write_text(
        '[server]\nworkspace = "/workspace/cairn"\n'
        f'data_root = "{tmp_path / "state"}"\n'
        f'worktrees_root = "{tmp_path / "trees"}"\n'
        'provider_approval = "/etc/cairn/provider-approval.json"\n'
    )
    (root / "deploy/systemd/cairn.service.in").write_text(
        Path("deploy/systemd/cairn.service.in").read_text()
    )
    (root / ".cairn").mkdir()
    (root / ".cairn/models.toml").write_text(
        '[[providers]]\nname = "fixture"\nbase_url = "https://example.invalid/v1"\n'
        'api_key_env = "BOOTSTRAP_TEST_KEY"\n'
        '[[providers.models]]\nname = "primary"\nmodel_ids = ["openai/fixture"]\n'
    )
    (root / ".env").write_text("BOOTSTRAP_TEST_KEY=private-test-value\n")
    current = pwd.getpwuid(os.getuid())
    account = pwd.struct_passwd(
        (
            current.pw_name,
            current.pw_passwd,
            current.pw_uid,
            current.pw_gid,
            current.pw_gecos,
            str(tmp_path / "home"),
            current.pw_shell,
        )
    )
    monkeypatch.setattr(pwd, "getpwnam", lambda _: account)
    # Only temporary paths are published; root admission is covered separately.
    monkeypatch.setattr(bootstrap, "require_file_owner", lambda _: None)
    for key in tuple(os.environ):
        if key.startswith("CAIRN_"):
            monkeypatch.delenv(key)
    return root, tmp_path / "host/config.toml", account.pw_name


def test_first_use_preserves_model_secrets_and_existing_host_files(
    host: tuple[Path, Path, str],
) -> None:
    root, config, user = host
    model = (root / ".cairn/models.toml").read_bytes()
    secrets = (root / ".env").read_bytes()
    bootstrap.prepare(root, config, user)
    approval = config.parent / "provider-approval.json"
    assert json.loads(approval.read_text())["model_ids"] == ["openai/fixture"]
    assert "private-test-value" not in config.read_text() + approval.read_text()
    assert config.stat().st_mode & 0o777 == 0o600
    assert approval.stat().st_mode & 0o777 == 0o600
    original = (config.read_bytes(), approval.read_bytes(), config.stat().st_ino)
    bootstrap.prepare(root, config, user)
    assert original == (
        config.read_bytes(),
        approval.read_bytes(),
        config.stat().st_ino,
    )
    assert (root / ".cairn/models.toml").read_bytes() == model
    assert (root / ".env").read_bytes() == secrets
    assert not load_server_config(config).data_root.exists()


@pytest.mark.parametrize("target", ["config", "approval", "parent-symlink"])
def test_invalid_policy_paths_publish_nothing(
    host: tuple[Path, Path, str], target: str
) -> None:
    root, config, user = host
    template = root / "deploy/config.example.toml"
    if target == "config":
        config = root / "host/config.toml"
    elif target == "approval":
        template.write_text(
            template.read_text().replace(
                "/etc/cairn/provider-approval.json", str(root / "approval.json")
            )
        )
    else:
        config.parent.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match=r"Host|symlink"):
        bootstrap.prepare(root, config, user)
    assert not config.exists()
    assert not (root / "approval.json").exists()
    assert not (config.parent / "provider-approval.json").exists()


def test_failed_second_publication_rolls_back_only_new_config(
    host: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, user = host
    publish = bootstrap.publish_file

    def fail_approval(path: Path, content: str, mode: int = 0o600) -> os.stat_result:
        if path.name == "provider-approval.json":
            raise OSError("simulated disk error")
        return publish(path, content, mode)

    monkeypatch.setattr(bootstrap, "publish_file", fail_approval)
    with pytest.raises(OSError, match="disk error"):
        bootstrap.prepare(root, config, user)
    assert not config.exists()
    assert not tuple(config.parent.glob("*.tmp"))


def test_service_home_legacy_state_is_pinned_independently_of_caller_xdg(
    host: tuple[Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, user = host
    legacy = tmp_path / "home/.local/state/cairn"
    legacy.mkdir(parents=True)
    marker = legacy / "history.json"
    marker.write_text('{"retained": true}')
    template = root / "deploy/config.example.toml"
    template.write_text(
        template.read_text()
        .replace(str(tmp_path / "state"), "/var/lib/cairn")
        .replace(str(tmp_path / "trees"), "/var/lib/cairn/worktrees")
    )
    # The real /var/lib may contain operator state: hide only that probe.
    original = Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: False if path == Path("/var/lib/cairn") else original(path),
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "wrong-user-state"))
    bootstrap.prepare(root, config, user)
    settings = load_server_config(config)
    assert settings.data_root == legacy
    assert settings.worktrees_root == legacy / "worktrees"
    assert marker.read_text() == '{"retained": true}'
    assert not (tmp_path / "wrong-user-state").exists()


@pytest.mark.parametrize("uid", [1001, 0])
def test_nonroot_and_dedicated_user_defaults_are_refused(
    monkeypatch: pytest.MonkeyPatch, uid: int
) -> None:
    account = pwd.struct_passwd(
        ("service-user", "x", 1002, 1002, "", "/home/service-user", "/bin/false")
    )
    monkeypatch.setattr(os, "geteuid", lambda: uid)
    with pytest.raises((PermissionError, ValueError), match=r"root|dedicated"):
        bootstrap.require_file_owner(account)


def test_unit_install_is_exclusive_and_rendered_for_selected_user(
    host: tuple[Path, Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, user = host
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(bootstrap, "SYSTEM_UNIT_DIRECTORY", tmp_path / "units")
    bootstrap.install_unit(root, config, "cairn-isolated.service", user)
    unit = tmp_path / "units/cairn-isolated.service"
    original = unit.read_bytes()
    assert f"User={user}" in original.decode()
    assert f'--config "{config}"' in original.decode()
    with pytest.raises(FileExistsError):
        bootstrap.install_unit(root, config, "cairn-isolated.service", user)
    assert unit.read_bytes() == original
