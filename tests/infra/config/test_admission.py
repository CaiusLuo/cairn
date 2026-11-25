"""Host approval binds every model group the runtime may expose."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from cairn.agent.models.catalog import NamedProvider
from cairn.agent.models.config.admission import (
    ApprovedGroup,
    ProviderAllowlist,
    ProviderApproval,
    ProviderApprovalMismatch,
    approval_matches,
    approved_groups,
    expected_catalog_allowlist,
    expected_catalog_approval,
    load_approved_catalog,
    load_approved_provider,
    parse_provider_allowlist,
    parse_provider_approval,
)
from cairn.agent.models.config.loader import load_project_providers

CATALOG = """\
[[providers]]
name = "primary"
base_url = "https://example.test/v1"
api_key_env = "ADMISSION_KEY"

[[providers.models]]
name = "fast"
model_ids = ["fast-1", "fast-2"]

[[providers.models]]
name = "deep"
model_ids = ["deep-1"]

[[providers.models]]
name = "unapproved"
model_ids = ["hidden-1"]
"""


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / ".cairn").mkdir(parents=True)
    (workspace / ".cairn/models.toml").write_text(CATALOG)
    (workspace / ".env").write_text("ADMISSION_KEY=private-test-key\n")
    return workspace


def _provider(workspace: Path) -> NamedProvider:
    return load_project_providers(workspace / ".cairn/models.toml").providers[0]


def test_catalog_approval_filters_and_orders_runtime_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ADMISSION_KEY", raising=False)
    workspace = _workspace(tmp_path)
    provider = _provider(workspace)
    approval = ProviderApproval(
        name="primary",
        endpoint="https://example.test/v1",
        group="deep",
        key_env="ADMISSION_KEY",
        model_ids=("deep-1",),
        groups=(
            ApprovedGroup("deep", ("deep-1",)),
            ApprovedGroup("fast", ("fast-1", "fast-2")),
        ),
    )

    approved = load_approved_provider(workspace, approval)

    assert [group.name for group in approved.provider.config.model_config] == [
        "deep",
        "fast",
    ]
    assert approved.group.name == "deep"
    assert approved.api_key == "private-test-key"
    assert "hidden-1" not in {
        ident
        for group in approved.provider.config.model_config
        for ident in group.model_ids
    }
    assert approved_groups(approval, provider) is not None
    assert approval_matches(approval, provider)


def test_legacy_approval_keeps_only_the_first_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ADMISSION_KEY", raising=False)
    workspace = _workspace(tmp_path)
    approval = ProviderApproval(
        name="primary",
        endpoint="https://example.test/v1",
        group="fast",
        key_env="ADMISSION_KEY",
        model_ids=("fast-1", "fast-2"),
    )

    parsed = parse_provider_approval(
        {
            "name": "primary",
            "endpoint": "https://example.test/v1",
            "group": "fast",
            "key_env": "ADMISSION_KEY",
            "model_ids": ["fast-1", "fast-2"],
        }
    )
    approved = load_approved_provider(workspace, parsed)

    assert parsed == approval
    assert [group.name for group in approved.provider.config.model_config] == ["fast"]


def test_legacy_approval_of_a_later_group_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ADMISSION_KEY", raising=False)
    workspace = _workspace(tmp_path)
    approval = ProviderApproval(
        name="primary",
        endpoint="https://example.test/v1",
        group="deep",
        key_env="ADMISSION_KEY",
        model_ids=("deep-1",),
    )

    with pytest.raises(ProviderApprovalMismatch):
        load_approved_provider(workspace, approval)


def test_changed_group_ids_invalidate_the_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ADMISSION_KEY", raising=False)
    workspace = _workspace(tmp_path)
    provider = _provider(workspace)
    approval = expected_catalog_approval(provider)
    (workspace / ".cairn/models.toml").write_text(
        CATALOG.replace('model_ids = ["deep-1"]', 'model_ids = ["deep-2"]')
    )

    with pytest.raises(ProviderApprovalMismatch):
        load_approved_provider(workspace, approval)


def test_catalog_approval_serializes_only_public_facts(tmp_path: Path) -> None:
    provider = _provider(_workspace(tmp_path))

    approval = expected_catalog_approval(provider)
    encoded = json.dumps(
        {
            "name": approval.name,
            "endpoint": approval.endpoint,
            "group": approval.group,
            "key_env": approval.key_env,
            "model_ids": list(approval.model_ids),
            "groups": [
                {"name": group.name, "model_ids": list(group.model_ids)}
                for group in approval.groups
            ],
        }
    )

    assert "private-test-key" not in encoded
    assert parse_provider_approval(json.loads(encoded)) == approval
    assert approval.groups[0] == ApprovedGroup("fast", ("fast-1", "fast-2"))


@pytest.mark.parametrize(
    "groups",
    [
        [],
        [{"name": "fast"}],
        [{"name": "fast", "model_ids": []}],
        [
            {"name": "fast", "model_ids": ["fast-1"]},
            {"name": "fast", "model_ids": ["fast-2"]},
        ],
        [{"name": "deep", "model_ids": ["deep-1"]}],
    ],
)
def test_parse_rejects_invalid_group_lists(groups: list[object]) -> None:
    data = {
        "name": "primary",
        "endpoint": "https://example.test/v1",
        "group": "fast",
        "key_env": "ADMISSION_KEY",
        "model_ids": ["fast-1", "fast-2"],
        "groups": groups,
    }

    with pytest.raises(ValueError):
        parse_provider_approval(data)


MULTI_CATALOG = """\
[[providers]]
name = "primary"
base_url = "https://primary.test/v1"
api_key_env = "PRIMARY_KEY"

[[providers.models]]
name = "fast"
model_ids = ["fast-1"]

[[providers]]
name = "secondary"
base_url = "https://secondary.test/v1"
api_key_env = "SECONDARY_KEY"

[[providers.models]]
name = "deep"
model_ids = ["deep-1", "deep-2"]

[[providers]]
name = "unapproved"
base_url = "https://hidden.test/v1"
api_key_env = "HIDDEN_KEY"

[[providers.models]]
name = "hidden"
model_ids = ["hidden-1"]
"""


def test_allowlist_v2_round_trips_and_tracks_every_provider(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".cairn/models.toml").write_text(MULTI_CATALOG)
    providers = load_project_providers(workspace / ".cairn/models.toml").providers

    allowlist = expected_catalog_allowlist(providers)
    encoded = json.dumps(asdict(allowlist))
    parsed = parse_provider_allowlist(json.loads(encoded))

    assert parsed == allowlist
    assert [approval.name for approval in parsed.providers] == [
        "primary",
        "secondary",
        "unapproved",
    ]
    secondary_approval = parsed.by_name("secondary")
    assert secondary_approval is not None
    assert secondary_approval.model_ids == ("deep-1", "deep-2")
    assert parsed.by_name("missing") is None
    legacy = parse_provider_allowlist(
        {
            "name": "primary",
            "endpoint": "https://primary.test/v1",
            "group": "fast",
            "key_env": "PRIMARY_KEY",
            "model_ids": ["fast-1"],
        }
    )
    assert legacy.version == 1
    assert [approval.name for approval in legacy.providers] == ["primary"]


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "providers": []},
        {"version": 2, "providers": []},
        {"providers": [{"name": "only"}]},
        {"version": 2, "providers": [], "extra": True},
    ],
)
def test_allowlist_rejects_malformed_catalogs(payload: object) -> None:
    with pytest.raises(ValueError):
        parse_provider_allowlist(payload)


def test_catalog_covers_every_approved_provider_and_keeps_missing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.delenv("SECONDARY_KEY", raising=False)
    workspace = _workspace(tmp_path)
    (workspace / ".cairn/models.toml").write_text(MULTI_CATALOG)
    (workspace / ".env").write_text("PRIMARY_KEY=primary-secret\n")
    providers = load_project_providers(workspace / ".cairn/models.toml").providers
    allowlist = expected_catalog_allowlist(providers)

    catalog = load_approved_catalog(workspace, allowlist, host_env={})

    assert [approved.provider.name for approved in catalog.providers] == [
        "primary",
        "secondary",
        "unapproved",
    ]
    secondary = catalog.by_name("secondary")
    assert secondary is not None
    assert secondary.api_key is None
    assert secondary.missing_credential == "SECONDARY_KEY"
    assert [group.name for group in secondary.provider.config.model_config] == ["deep"]
    assert "primary-secret" not in repr(catalog)
    assert "PRIMARY_KEY" in catalog.secret_env_keys


def test_catalog_refuses_startup_when_no_provider_has_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    monkeypatch.delenv("SECONDARY_KEY", raising=False)
    workspace = _workspace(tmp_path)
    (workspace / ".cairn/models.toml").write_text(MULTI_CATALOG)
    (workspace / ".env").write_text("")
    providers = load_project_providers(workspace / ".cairn/models.toml").providers

    with pytest.raises(ProviderApprovalMismatch):
        load_approved_catalog(
            workspace, expected_catalog_allowlist(providers), host_env={}
        )


def test_catalog_drops_providers_the_allowlist_does_not_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRIMARY_KEY", raising=False)
    workspace = _workspace(tmp_path)
    (workspace / ".cairn/models.toml").write_text(MULTI_CATALOG)
    (workspace / ".env").write_text(
        "PRIMARY_KEY=primary-secret\nHIDDEN_KEY=hidden-secret\n"
    )
    approval = ProviderApproval(
        name="primary",
        endpoint="https://primary.test/v1",
        group="fast",
        key_env="PRIMARY_KEY",
        model_ids=("fast-1",),
    )

    catalog = load_approved_catalog(
        workspace, ProviderAllowlist((approval,)), host_env={}
    )

    assert [approved.provider.name for approved in catalog.providers] == ["primary"]
    assert "HIDDEN_KEY" not in catalog.secret_env_keys
    assert "hidden-secret" not in catalog.sensitive_values
