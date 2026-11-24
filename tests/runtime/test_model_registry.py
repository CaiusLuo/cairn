"""Runtime model selection is separate from the approved provider catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.agent.context.window import ContextBudget
from cairn.agent.models.catalog import NamedProvider
from cairn.agent.models.config.admission import ApprovedCatalog, ApprovedProvider
from cairn.agent.models.registry import ModelSelectionError, ProviderRegistry
from cairn.agent.models.selection import ModelConfig, ModelRef, ProviderConfig
from cairn.agent.models.selection_store import ModelSelectionStore

BUDGET = ContextBudget(max_tokens=100, response_tokens=20)


def _approved(
    name: str,
    *,
    models: tuple[str, ...] = ("model-a",),
    missing_credential: str | None = None,
) -> ApprovedProvider:
    provider = NamedProvider(
        name=name,
        config=ProviderConfig(
            base_url=f"https://{name}.test/v1",
            api_key_env=f"{name.upper()}_KEY",
            model_config=(ModelConfig("chat", models),),
        ),
    )
    return ApprovedProvider(
        provider=provider,
        group=provider.config.model_config[0],
        api_key=None if missing_credential else "credential",
        secret_env_keys=frozenset({f"{name.upper()}_KEY"}),
        sensitive_values=(),
        context_budget=BUDGET,
        missing_credential=missing_credential,
    )


def _catalog() -> ApprovedCatalog:
    return ApprovedCatalog(
        providers=(
            _approved("primary", models=("a", "b")),
            _approved("secondary", models=("c",)),
        ),
        secret_env_keys=frozenset({"PRIMARY_KEY", "SECONDARY_KEY"}),
        sensitive_values=(),
    )


def test_registry_switches_between_approved_providers_without_a_new_catalog() -> None:
    registry = ProviderRegistry(_catalog(), ModelRef("primary", "chat", "a"))

    secondary = registry.prepare(ModelRef("secondary", "chat", "c"))
    assert registry.current_ref() == ModelRef("primary", "chat", "a")

    registry.commit(secondary)

    assert registry.current_ref() == ModelRef("secondary", "chat", "c")
    assert registry.current_activation().runtime.provider.name == "secondary"
    assert {choice.ref for choice in registry.choices()} == {
        ModelRef("primary", "chat", "a"),
        ModelRef("primary", "chat", "b"),
        ModelRef("secondary", "chat", "c"),
    }


def test_registry_rejects_unapproved_providers_and_groups() -> None:
    registry = ProviderRegistry(_catalog(), ModelRef("primary", "chat", "a"))

    with pytest.raises(ModelSelectionError):
        registry.prepare(ModelRef("hidden", "chat", "a"))
    with pytest.raises(ModelSelectionError):
        registry.prepare(ModelRef("primary", "chat", "outside"))


def test_stale_saved_selection_falls_back_to_the_catalog_first_model() -> None:
    registry = ProviderRegistry(_catalog(), ModelRef("gone", "chat", "a"))

    assert registry.current_ref() == ModelRef("primary", "chat", "a")
    assert registry.current_activation().runtime.provider.name == "primary"


def test_default_prefers_a_provider_with_a_configured_credential() -> None:
    catalog = ApprovedCatalog(
        providers=(
            _approved("unready", missing_credential="UNREADY_KEY"),
            _approved("ready"),
        ),
        secret_env_keys=frozenset({"UNREADY_KEY", "READY_KEY"}),
        sensitive_values=(),
    )

    registry = ProviderRegistry(catalog, None)

    assert registry.current_ref() == ModelRef("ready", "chat", "model-a")
    # The unready provider stays selectable; using it fails clearly at request
    # time instead of being silently hidden.
    assert {choice.ref.provider for choice in registry.choices()} == {
        "unready",
        "ready",
    }


def test_preparing_a_new_catalog_does_not_publish_it() -> None:
    original = _catalog()
    registry = ProviderRegistry(original, ModelRef("primary", "chat", "a"))
    replacement = ApprovedCatalog(
        providers=(_approved("replacement"),),
        secret_env_keys=frozenset({"REPLACEMENT_KEY"}),
        sensitive_values=(),
    )

    activation = registry.prepare_catalog(
        replacement, ModelRef("secondary", "chat", "c")
    )

    assert activation.ref == ModelRef("replacement", "chat", "model-a")
    assert registry.current_ref() == ModelRef("primary", "chat", "a")
    assert registry.catalog is original

    registry.install_prepared(replacement, activation)

    assert registry.current_ref() == ModelRef("replacement", "chat", "model-a")
    assert registry.catalog is replacement
    assert registry.current_activation().runtime.provider.name == "replacement"


def test_find_prefers_the_current_provider_for_ambiguous_model_ids() -> None:
    catalog = ApprovedCatalog(
        providers=(
            _approved("primary", models=("shared", "only-primary")),
            _approved("secondary", models=("shared", "only-secondary")),
        ),
        secret_env_keys=frozenset(),
        sensitive_values=(),
    )
    registry = ProviderRegistry(catalog, ModelRef("secondary", "chat", "shared"))

    assert registry.find("shared") == ModelRef("secondary", "chat", "shared")
    assert registry.find("only-primary") == ModelRef("primary", "chat", "only-primary")
    assert registry.find("shared", provider="primary") == ModelRef(
        "primary", "chat", "shared"
    )


def test_selection_store_round_trips_default_and_roles(tmp_path: Path) -> None:
    store = ModelSelectionStore(tmp_path / "state/model-selection.json")
    default = ModelRef("primary", "chat", "a")
    embedding = ModelRef("secondary", "embed", "e")

    store.save(default)
    store.save(embedding, role="embedding")

    loaded = store.load()
    assert loaded.default == default
    assert loaded.for_role("embedding") == embedding
    assert loaded.for_role("missing") == default
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_selection_store_degrades_missing_and_corrupt_state(tmp_path: Path) -> None:
    store = ModelSelectionStore(tmp_path / "state/model-selection.json")
    assert store.load().default is None

    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"version": 1, "default": "broken"}))
    assert store.load().default is None

    store.path.write_text("{not json")
    assert store.load().default is None


def test_selection_store_rejects_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    store = ModelSelectionStore(tmp_path / "state/model-selection.json")
    store.path.parent.mkdir(parents=True)
    store.path.symlink_to(target)

    with pytest.raises(ValueError):
        store.load()
    with pytest.raises(ValueError):
        store.save(ModelRef("primary", "chat", "a"))
