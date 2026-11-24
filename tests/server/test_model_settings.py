"""Model settings stay host-authorized, secret-free and restart-aware."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest
from dotenv import dotenv_values

import cairn.server.model_settings as settings_module
from cairn.agent.models.config.loader import load_project_providers
from cairn.agent.models.selection import ModelManager
from cairn.server.model_settings import (
    ModelSettingsError,
    ModelSettingsService,
    ProbeModels,
    SaveModelSettings,
)

TOML = """\
# Workspace provider catalog
[[providers]]
name = "bailian" # active provider
base_url = "https://old.example/v1"
api_key_env = "BAILIAN_KEY"

[[providers.models]]
name = "kimi" # first group
model_ids = ["openai/kimi"] # kimi id

[[providers.models]]
name = "deepseek"
model_ids = ["openai/deepseek"]

[[providers]]
name = "backup"
base_url = "https://backup.example/v1"
api_key_env = "BACKUP_KEY"

[[providers.models]]
name = "backup"
model_ids = ["backup/model"]
"""

LEGACY = """\
# Legacy provider
base_url = "https://old.example/v1"
api_key_env = "BAILIAN_KEY"

[[models]]
name = "kimi"
model_ids = ["openai/kimi"]
"""


def _runtime(path: Path) -> SimpleNamespace:
    project = load_project_providers(path)
    provider = project.providers[0]
    return SimpleNamespace(
        provider=provider,
        model_manager=ModelManager(provider.config),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "workspace"
    (root / ".cairn").mkdir(parents=True)
    config = root / ".cairn/models.toml"
    config.write_text(TOML)
    (root / ".env").write_text(
        "BAILIAN_KEY=dotenv-secret\nBACKUP_KEY=backup-secret-alt\nOTHER=keep-me\n"
    )
    approval = tmp_path / "policy/provider.json"
    approval.parent.mkdir()
    provider = load_project_providers(config).providers[0]
    groups = [
        {"name": group.name, "model_ids": list(group.model_ids)}
        for group in provider.config.model_config
    ]
    approval.write_text(
        json.dumps(
            {
                "name": provider.name,
                "endpoint": provider.config.base_url,
                "group": provider.config.model_config[0].name,
                "key_env": provider.config.api_key_env,
                "model_ids": list(provider.config.model_config[0].model_ids),
                "groups": groups,
            }
        )
    )
    return root, approval, config


def _service(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> ModelSettingsService:
    root, approval, config = workspace
    state = root.parent / "state"
    monkeypatch.delenv("BAILIAN_KEY", raising=False)
    monkeypatch.delenv("BACKUP_KEY", raising=False)
    # litellm imports load the repository .env into the test process.
    monkeypatch.delenv("CAIRN_LLM_API_KEY", raising=False)
    execution = SimpleNamespace(provider_runtime=_runtime(config))
    return ModelSettingsService(root, state, approval, execution)


def _update(**changes: Any) -> SaveModelSettings:
    values: dict[str, Any] = {
        "mode": "update",
        "active_provider": "bailian",
        "provider_name": "bailian",
        "base_url": "https://new.example/v1",
        "api_key_env": None,
        "api_key": "new-secret",
        "active_group": "kimi",
        "group_name": "kimi",
        "model_ids": ("openai/kimi",),
    }
    values.update(changes)
    return SaveModelSettings(**values)


def test_read_describes_files_and_running_provider_without_secrets(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(workspace, monkeypatch)

    result = service.read()
    encoded = json.dumps(result, ensure_ascii=False)

    assert result["layout"] == "catalog"
    assert result["active_provider"] == "bailian"
    assert result["active_group"] == "kimi"
    assert result["active_model"] == "openai/kimi"
    assert result["providers"][0]["key_source"] == "dotenv"
    assert result["providers"][0]["groups"][1]["next_start"] is False
    assert [group["approved"] for group in result["providers"][0]["groups"]] == [
        True,
        True,
    ]
    assert [group["approved"] for group in result["providers"][1]["groups"]] == [False]
    assert result["running"] == {
        "provider": "bailian",
        "group": "kimi",
        "model": "openai/kimi",
        "base_url": "https://old.example/v1",
    }
    assert result["restart_required"] is False
    assert result["can_save"] is True
    assert "old-secret" not in encoded
    assert "backup-secret" not in encoded


def test_save_switches_group_preserves_other_groups_and_providers(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, approval, config = workspace
    service = _service(workspace, monkeypatch)

    result = service.save(
        _update(
            active_group="deepseek",
            group_name="deepseek-fast",
            model_ids=("openai/deepseek-v2", "openai/deepseek-backup"),
        )
    )

    project = load_project_providers(config)
    provider = project.providers[0]
    assert project.layout == "catalog"
    assert provider.name == "bailian"
    assert provider.config.base_url == "https://new.example/v1"
    assert [group.name for group in provider.config.model_config] == [
        "deepseek-fast",
        "kimi",
    ]
    assert provider.config.model_config[0].model_ids == (
        "openai/deepseek-v2",
        "openai/deepseek-backup",
    )
    assert [item.name for item in project.providers] == ["bailian", "backup"]
    assert "# Workspace provider catalog" in config.read_text()
    assert "backup/model" in config.read_text()

    env = (root / ".env").read_text()
    assert "BAILIAN_KEY=new-secret" in env
    assert "BACKUP_KEY=backup-secret-alt" in env
    assert "OTHER=keep-me" in env
    assert (root / ".env").stat().st_mode & 0o777 == 0o600

    approved = json.loads(approval.read_text())
    assert approved == {
        "name": "bailian",
        "endpoint": "https://new.example/v1",
        "group": "deepseek-fast",
        "key_env": "BAILIAN_KEY",
        "model_ids": ["openai/deepseek-v2", "openai/deepseek-backup"],
        "groups": [
            {
                "name": "deepseek-fast",
                "model_ids": ["openai/deepseek-v2", "openai/deepseek-backup"],
            },
            {"name": "kimi", "model_ids": ["openai/kimi"]},
        ],
    }
    assert result["restart_required"] is True
    assert result["settings"]["active_group"] == "deepseek-fast"
    assert result["settings"]["restart_required"] is True
    assert "new-secret" not in json.dumps(result)
    assert (root.parent / "state/model-settings-pending.json").is_file()


def test_save_replaces_every_duplicate_credential_line(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = workspace
    env_path = root / ".env"
    env_path.write_text("BAILIAN_KEY=first-old\nOTHER=keep-me\nBAILIAN_KEY=last-old\n")
    service = _service(workspace, monkeypatch)
    expected = _update().api_key
    assert expected is not None

    service.save(_update())

    text = env_path.read_text()
    assert "first-old" not in text
    assert "last-old" not in text
    assert text.count(f"BAILIAN_KEY={expected}") == 2
    assert "OTHER=keep-me" in text
    assert dotenv_values(env_path)["BAILIAN_KEY"] == expected


def test_key_only_change_stays_pending_until_a_restart(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(workspace, monkeypatch)
    assert service.read()["restart_required"] is False

    result = service.save(_update(base_url="https://old.example/v1"))

    assert result["settings"]["restart_required"] is True
    assert (workspace[0].parent / "state/model-settings-pending.json").exists()
    service.clear_pending()
    assert service.read()["restart_required"] is False
    assert (workspace[0] / ".env").read_text().startswith("BAILIAN_KEY=new-secret\n")


def test_create_uses_a_free_credential_name_and_keeps_existing_providers(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, approval, config = workspace
    service = _service(workspace, monkeypatch)

    service.save(
        SaveModelSettings(
            mode="create",
            active_provider="",
            provider_name="magpie",
            base_url="http://127.0.0.1:3425/v1",
            api_key_env=None,
            api_key="local",
            active_group="",
            group_name="default",
            model_ids=("gpt-4o-mini",),
        )
    )

    project = load_project_providers(config)
    assert [item.name for item in project.providers] == [
        "magpie",
        "bailian",
        "backup",
    ]
    assert project.providers[0].config.api_key_env == "CAIRN_LLM_API_KEY"
    assert "CAIRN_LLM_API_KEY=local" in (root / ".env").read_text()
    assert json.loads(approval.read_text())["name"] == "magpie"


def test_create_refuses_a_credential_name_used_by_another_provider(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(workspace, monkeypatch)
    before = {
        path: path.read_bytes()
        for path in (workspace[2], workspace[0] / ".env", workspace[1])
    }

    with pytest.raises(ModelSettingsError) as caught:
        service.save(
            SaveModelSettings(
                mode="create",
                active_provider="",
                provider_name="other",
                base_url="https://other.example/v1",
                api_key_env="BAILIAN_KEY",
                api_key="other-secret",
                active_group="",
                group_name="default",
                model_ids=("other/model",),
            )
        )

    assert caught.value.code == "invalid_model_settings"
    assert {
        path: path.read_bytes()
        for path in (workspace[2], workspace[0] / ".env", workspace[1])
    } == before


def test_host_environment_credential_is_not_overwritten(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(workspace, monkeypatch)
    monkeypatch.setenv("BAILIAN_KEY", "host-managed")
    (workspace[0] / ".env").write_text("BACKUP_KEY=backup-secret-alt\nOTHER=keep-me")
    before = (workspace[2].read_bytes(), (workspace[0] / ".env").read_bytes())

    with pytest.raises(ModelSettingsError) as caught:
        service.save(_update())

    assert caught.value.code == "host_env_credential"
    assert (workspace[2].read_bytes(), (workspace[0] / ".env").read_bytes()) == before
    assert service.read()["providers"][0]["key_source"] == "host"


def test_workspace_env_key_can_be_replaced_even_if_exported(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = workspace
    service = _service(workspace, monkeypatch)
    monkeypatch.setenv("BAILIAN_KEY", "host-stale")
    expected = _update().api_key
    assert expected is not None
    assert service.read()["providers"][0]["key_source"] == "dotenv"

    result = service.save(_update())

    assert result["restart_required"] is True
    text = (root / ".env").read_text()
    assert f"BAILIAN_KEY={expected}" in text
    assert "host-stale" not in text


def test_invalid_endpoint_and_missing_credential_write_nothing(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(workspace, monkeypatch)
    before = workspace[2].read_bytes()

    with pytest.raises(ModelSettingsError) as caught:
        service.save(_update(base_url="http://example.com/v1"))
    assert caught.value.code == "invalid_model_settings"
    assert workspace[2].read_bytes() == before

    (workspace[0] / ".env").write_text("BACKUP_KEY=backup-secret-alt")
    with pytest.raises(ModelSettingsError) as caught:
        service.save(_update(api_key=None))
    assert caught.value.code == "credential_missing"
    assert workspace[2].read_bytes() == before


@pytest.mark.parametrize("target", ["config", "env", "approval"])
def test_symlinks_are_refused_before_any_write(
    workspace: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    root, approval, config = workspace
    service = _service(workspace, monkeypatch)
    if target == "config":
        real = root / "real-models.toml"
        real.write_bytes(config.read_bytes())
        config.unlink()
        config.symlink_to(real)
        original = real.read_bytes()
    elif target == "env":
        real = root / "real.env"
        real.write_bytes((root / ".env").read_bytes())
        (root / ".env").unlink()
        (root / ".env").symlink_to(real)
        original = real.read_bytes()
    else:
        real = approval.parent / "real.json"
        real.write_bytes(approval.read_bytes())
        approval.unlink()
        approval.symlink_to(real)
        original = real.read_bytes()

    with pytest.raises(ModelSettingsError):
        service.save(_update())

    if target == "config":
        assert (root / "real-models.toml").read_bytes() == original
    elif target == "env":
        assert (root / "real.env").read_bytes() == original
    else:
        assert (approval.parent / "real.json").read_bytes() == original


def test_failed_approval_write_rolls_back_config_and_env(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, approval, config = workspace
    service = _service(workspace, monkeypatch)
    before = (config.read_bytes(), (root / ".env").read_bytes())
    before_approval = approval.read_bytes()
    real_write = settings_module._write_file

    def fail_approval(
        path: Path, content: bytes, mode: int, expected: bytes | None
    ) -> None:
        if path == approval:
            raise OSError("simulated disk error")
        real_write(path, content, mode, expected)

    monkeypatch.setattr(settings_module, "_write_file", fail_approval)

    with pytest.raises(OSError, match="disk error"):
        service.save(_update())

    assert (config.read_bytes(), (root / ".env").read_bytes()) == before
    assert approval.read_bytes() == before_approval
    assert not (root.parent / "state/model-settings-pending.json").exists()


def test_legacy_update_stays_legacy_and_rename_converts_to_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    (root / ".cairn").mkdir(parents=True)
    config = root / ".cairn/models.toml"
    config.write_text(LEGACY)
    (root / ".env").write_text("BAILIAN_KEY=legacy-secret\n")
    approval = tmp_path / "policy/provider.json"
    approval.parent.mkdir()
    provider = load_project_providers(config).providers[0]
    approval.write_text(
        json.dumps(
            {
                "name": "default",
                "endpoint": provider.config.base_url,
                "group": "kimi",
                "key_env": "BAILIAN_KEY",
                "model_ids": ["openai/kimi"],
            }
        )
    )
    monkeypatch.delenv("BAILIAN_KEY", raising=False)
    execution = SimpleNamespace(provider_runtime=_runtime(config))
    service = ModelSettingsService(root, tmp_path / "state", approval, execution)

    service.save(
        _update(
            active_provider="default",
            provider_name="default",
            base_url="https://new.example/v1",
            api_key="new-secret",
        )
    )
    assert "[[providers]]" not in config.read_text()
    assert load_project_providers(config).layout.value == "legacy"

    service.save(
        _update(
            active_provider="default",
            provider_name="renamed",
            base_url="https://new.example/v1",
            api_key="new-secret",
        )
    )
    project = load_project_providers(config)
    assert project.layout.value == "catalog"
    assert project.providers[0].name == "renamed"


def test_env_layout_create_writes_a_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".env").write_text("CAIRN_LLM_API_KEY=env-secret\n")
    approval = tmp_path / "policy/provider.json"
    approval.parent.mkdir()
    monkeypatch.delenv("CAIRN_LLM_API_KEY", raising=False)
    execution = SimpleNamespace(provider_runtime=None)
    service = ModelSettingsService(root, tmp_path / "state", approval, execution)

    assert service.read()["layout"] == "env"
    service.save(
        SaveModelSettings(
            mode="create",
            active_provider="",
            provider_name="deepseek",
            base_url="https://api.deepseek.com/v1",
            api_key_env=None,
            api_key="new-secret",
            active_group="",
            group_name="deepseek-chat",
            model_ids=("deepseek-chat",),
        )
    )
    project = load_project_providers(root / ".cairn/models.toml")
    assert project.providers[0].name == "deepseek"
    assert project.providers[0].config.model_config[0].model_ids == ("deepseek-chat",)
    assert json.loads(approval.read_text())["name"] == "deepseek"


def test_read_after_external_change_requires_a_restart(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(workspace, monkeypatch)
    config = workspace[2]
    config.write_text(
        config.read_text().replace("https://old.example/v1", "https://other.example/v1")
    )

    settings = service.read()

    assert settings["restart_required"] is True


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def iter_bytes(self) -> Any:
        yield self._body


class _FakeClient:
    seen: ClassVar[list[tuple[str, dict[str, str]]]] = []
    status = 200
    body = b'{"data": [{"id": "model-a"}, {"id": "model-b"}]}'

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def stream(self, method: str, url: str, headers: dict[str, str]) -> _FakeResponse:
        self.seen.append((url, headers))
        return _FakeResponse(self.status, self.body)


def test_probe_returns_model_ids_without_exposing_the_key(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(workspace, monkeypatch)
    _FakeClient.seen = []
    _FakeClient.status = 200
    _FakeClient.body = b'{"data": [{"id": "model-a"}, {"id": "model-b"}]}'
    monkeypatch.setattr(httpx, "Client", _FakeClient)

    result = service.probe(
        ProbeModels(
            base_url="https://probe.example/v1",
            api_key_env=None,
            api_key="probe-secret",
        )
    )

    assert result == {
        "endpoint": "https://probe.example/v1/models",
        "models": ["model-a", "model-b"],
    }
    assert _FakeClient.seen[0][0] == "https://probe.example/v1/models"
    assert _FakeClient.seen[0][1]["Authorization"] == "Bearer probe-secret"
    assert "probe-secret" not in json.dumps(result)


def test_probe_prefers_the_workspace_credential(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(workspace, monkeypatch)
    monkeypatch.setenv("BAILIAN_KEY", "host-stale")
    _FakeClient.seen = []
    _FakeClient.status = 200
    _FakeClient.body = b'{"data": [{"id": "model-a"}]}'
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    stored = dotenv_values(workspace[0] / ".env")["BAILIAN_KEY"]

    service.probe(
        ProbeModels(
            base_url="https://probe.example/v1",
            api_key_env="BAILIAN_KEY",
            api_key=None,
        )
    )

    assert _FakeClient.seen[0][1]["Authorization"] == f"Bearer {stored}"


def test_probe_maps_rejected_credentials(
    workspace: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(workspace, monkeypatch)
    _FakeClient.status = 401
    monkeypatch.setattr(httpx, "Client", _FakeClient)

    with pytest.raises(ModelSettingsError) as caught:
        service.probe(
            ProbeModels(
                base_url="https://probe.example/v1",
                api_key_env=None,
                api_key="wrong",
            )
        )

    assert caught.value.code == "probe_credential_rejected"
    _FakeClient.status = 200
