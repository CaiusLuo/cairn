"""Web model settings require the host session and never echo credentials."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

import cairn.server.model_settings as settings_module
from cairn.agent.models.config.admission import ApprovedCatalog
from cairn.agent.models.config.loader import load_project_providers
from cairn.agent.models.registry import ModelSelectionError
from cairn.agent.models.runtime import (
    ProviderActivation,
    build_provider_runtime,
    runtime_for_model,
)
from cairn.agent.models.selection import ModelManager, ModelRef
from cairn.agent.models.selection_store import ModelSelectionStore
from cairn.interfaces.web.app import create_app
from cairn.interfaces.web.config import WebConfig
from cairn.interfaces.web.reader import SnapshotReader
from cairn.knowledge.store import KnowledgeStore
from cairn.server.model_settings import ModelSettingsService

TOML = """\
[[providers]]
name = "bailian"
base_url = "https://old.example/v1"
api_key_env = "BAILIAN_KEY"

[[providers.models]]
name = "kimi"
model_ids = ["openai/kimi"]
"""
API_KEY = "route-test-key"


class _Execution:
    def __init__(self, runtime: Any, store: ModelSelectionStore) -> None:
        self.provider_runtime = runtime
        self.selection_store = store
        self.catalog: ApprovedCatalog | None = None

    def current_model_ref(self) -> ModelRef | None:
        runtime = self.provider_runtime
        if runtime is None:
            return None
        group = runtime.model_manager.current_model()
        return ModelRef(
            runtime.provider.name,
            group.name,
            runtime.model_manager.current_model_id(),
        )

    def model_catalog_signature(self) -> tuple[tuple[object, ...], ...]:
        if self.catalog is not None:
            return tuple(
                (
                    approved.provider.name,
                    approved.provider.config.base_url,
                    approved.provider.config.api_key_env,
                    tuple(
                        (group.name, tuple(group.model_ids))
                        for group in approved.provider.config.model_config
                    ),
                )
                for approved in self.catalog.providers
            )
        runtime = self.provider_runtime
        if runtime is None:
            return ()
        return (
            (
                runtime.provider.name,
                runtime.provider.config.base_url,
                runtime.provider.config.api_key_env,
                tuple(
                    (group.name, tuple(group.model_ids))
                    for group in runtime.model_manager.list_models()
                ),
            ),
        )

    def install_provider_activation(self, activation: Any) -> None:
        self.provider_runtime = activation.runtime

    def install_model_catalog(
        self,
        catalog: ApprovedCatalog,
        selection: ModelRef | None,
        *,
        persist: bool = True,
    ) -> ProviderActivation:
        approved = (
            catalog.by_name(selection.provider)
            if selection is not None
            else catalog.providers[0]
        )
        assert approved is not None
        group = (
            next(
                group
                for group in approved.provider.config.model_config
                if group.name == selection.group
            )
            if selection is not None
            else approved.group
        )
        runtime = build_provider_runtime(
            approved.provider,
            approved.api_key,
            approved.context_budget,
            group=group.name,
            missing_credential=approved.missing_credential,
        )
        if (
            selection is not None
            and runtime.model_manager.current_model_id() != selection.model
        ):
            runtime = runtime_for_model(runtime, selection.model)
        ref = selection or ModelRef(
            approved.provider.name, group.name, group.model_ids[0]
        )
        activation = ProviderActivation(
            runtime=runtime,
            secret_env_keys=catalog.secret_env_keys,
            sensitive_values=catalog.sensitive_values,
            ref=ref,
        )
        self.catalog = catalog
        self.provider_runtime = runtime
        if persist:
            self.selection_store.save(ref)
        return activation

    def select_model(
        self, ref: ModelRef, *, persist: bool = True
    ) -> ProviderActivation:
        if self.catalog is None or self.catalog.by_name(ref.provider) is None:
            raise ModelSelectionError("Provider is not in the approved catalog")
        activation = self.install_model_catalog(self.catalog, ref, persist=persist)
        return activation


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    (root / ".cairn").mkdir(parents=True)
    config = root / ".cairn/models.toml"
    config.write_text(TOML)
    (root / ".env").write_text("BAILIAN_KEY=dotenv-route-key")
    provider = load_project_providers(config).providers[0]
    approval = tmp_path / "policy/provider.json"
    approval.parent.mkdir()
    approval.write_text(
        json.dumps(
            {
                "name": provider.name,
                "endpoint": provider.config.base_url,
                "group": provider.config.model_config[0].name,
                "key_env": provider.config.api_key_env,
                "model_ids": list(provider.config.model_config[0].model_ids),
            }
        )
    )
    return root, approval


def _app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Path, Path]:
    root, approval = _workspace(tmp_path)
    monkeypatch.delenv("BAILIAN_KEY", raising=False)
    provider = load_project_providers(root / ".cairn/models.toml").providers[0]
    runtime = SimpleNamespace(
        provider=provider,
        model_manager=ModelManager(provider.config),
    )
    service = ModelSettingsService(
        root,
        tmp_path / "state",
        approval,
        _Execution(
            runtime, ModelSelectionStore(tmp_path / "state/model-selection.json")
        ),
    )
    config = WebConfig(
        workspace=root,
        tasks_dir=tmp_path / "state/tasks",
        auth_passphrase="correct-horse",
    )
    app = create_app(
        config,
        reader=cast(
            SnapshotReader,
            SimpleNamespace(snapshot=lambda: {"read_only": True}),
        ),
        runtime_token="host-token",
        knowledge_store=cast(KnowledgeStore, SimpleNamespace()),
        model_settings=service,
    )
    return TestClient(app, base_url="http://127.0.0.1"), root, approval


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/login",
        json={"username": "cairn", "auth_passphrase": "correct-horse"},
    )
    assert response.status_code == 200


def _save_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "mode": "update",
        "active_provider": "bailian",
        "provider_name": "bailian",
        "base_url": "https://new.example/v1",
        "api_key_env": None,
        "api_key": API_KEY,
        "active_group": "kimi",
        "group_name": "kimi",
        "model_ids": ["openai/kimi-v2"],
    }
    body.update(changes)
    return body


def test_model_reads_and_writes_require_the_web_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _app(tmp_path, monkeypatch)

    assert client.get("/api/model").status_code == 401
    assert (
        client.put(
            "/api/model",
            headers={"Origin": "http://127.0.0.1"},
            json=_save_body(),
        ).status_code
        == 401
    )

    _login(client)
    assert client.get("/api/model").status_code == 200
    rejected = client.put(
        "/api/model",
        headers={"Origin": "http://evil.example"},
        json=_save_body(),
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"] == "invalid_origin"


def test_save_requires_json_and_never_echoes_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root, approval = _app(tmp_path, monkeypatch)
    _login(client)

    wrong_type = client.put(
        "/api/model",
        headers={"Origin": "http://127.0.0.1"},
        content="not-json",
    )
    assert wrong_type.status_code == 415

    response = client.put(
        "/api/model",
        headers={"Origin": "http://127.0.0.1"},
        json=_save_body(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["restart_required"] is False
    assert body["settings"]["active_group"] == "kimi"
    assert body["settings"]["running"]["base_url"] == "https://new.example/v1"
    assert API_KEY not in response.text
    assert API_KEY in (root / ".env").read_text()
    allowlist = json.loads(approval.read_text())
    assert allowlist["version"] == 2
    assert allowlist["providers"][0]["name"] == "bailian"
    assert allowlist["providers"][0]["model_ids"] == ["openai/kimi-v2"]

    read = client.get("/api/model")
    assert read.status_code == 200
    assert API_KEY not in read.text
    assert API_KEY not in json.dumps(body["settings"], ensure_ascii=False)


def test_invalid_request_does_not_echo_the_submitted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _app(tmp_path, monkeypatch)
    _login(client)
    oversized = "x" * 5000

    response = client.put(
        "/api/model",
        headers={"Origin": "http://127.0.0.1"},
        json=_save_body(api_key=oversized),
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_model_settings"
    assert oversized not in response.text


def test_selection_switches_models_without_touching_deployment_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root, approval = _app(tmp_path, monkeypatch)
    _login(client)
    saved = client.put(
        "/api/model",
        headers={"Origin": "http://127.0.0.1"},
        json=_save_body(model_ids=["openai/kimi-v2", "openai/kimi-v3"]),
    )
    assert saved.status_code == 200
    config_before = (root / ".cairn/models.toml").read_bytes()
    approval_before = approval.read_bytes()

    denied = client.post(
        "/api/model/select",
        headers={"Origin": "http://evil.example"},
        json={"provider": "bailian", "group": "kimi", "model": "openai/kimi-v3"},
    )
    assert denied.status_code == 403

    response = client.post(
        "/api/model/select",
        headers={"Origin": "http://127.0.0.1"},
        json={"provider": "bailian", "group": "kimi", "model": "openai/kimi-v3"},
    )

    assert response.status_code == 200
    settings = response.json()["settings"]
    assert settings["active_provider"] == "bailian"
    assert settings["active_group"] == "kimi"
    assert settings["active_model"] == "openai/kimi-v3"
    assert settings["restart_required"] is False
    assert (root / ".cairn/models.toml").read_bytes() == config_before
    assert approval.read_bytes() == approval_before
    state = json.loads((tmp_path / "state/model-selection.json").read_text())
    assert state["default"]["model"] == "openai/kimi-v3"

    unknown = client.post(
        "/api/model/select",
        headers={"Origin": "http://127.0.0.1"},
        json={"provider": "elsewhere", "group": "kimi", "model": "openai/kimi-v3"},
    )
    assert unknown.status_code == 409
    assert unknown.json()["error"] == "invalid_model_selection"


def test_probe_is_write_authorized_and_returns_only_model_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _ = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        settings_module,
        "_probe_models",
        lambda base_url, key: {
            "endpoint": base_url.rstrip("/") + "/models",
            "models": ["model-a", "model-b"],
        },
    )
    _login(client)

    denied = client.post(
        "/api/model/probe",
        headers={"Origin": "http://evil.example"},
        json={"base_url": "https://probe.example/v1", "api_key": API_KEY},
    )
    assert denied.status_code == 403

    response = client.post(
        "/api/model/probe",
        headers={"Origin": "http://127.0.0.1"},
        json={"base_url": "https://probe.example/v1", "api_key": API_KEY},
    )

    assert response.status_code == 200
    assert response.json() == {
        "endpoint": "https://probe.example/v1/models",
        "models": ["model-a", "model-b"],
    }
    assert API_KEY not in response.text
