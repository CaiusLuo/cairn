"""Host approval binds every model group the runtime may expose."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.agent.models.catalog import NamedProvider
from cairn.agent.models.config.admission import (
    ApprovedGroup,
    ProviderApproval,
    ProviderApprovalMismatch,
    approval_matches,
    approved_groups,
    expected_catalog_approval,
    load_approved_provider,
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
