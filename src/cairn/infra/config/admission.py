"""Host approval before credential resolution, shared by all execution entries."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from cairn.core.context import ContextBudget
from cairn.core.model_manager import ModelConfig
from cairn.core.provider_catalog import NamedProvider
from cairn.infra.config import (
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)


@dataclass(frozen=True, slots=True)
class ProviderApproval:
    """The exact provider facts an operator approves, bound before any credential.

    Binding identity, endpoint, model group, credential *variable name* and the
    full ordered model ids means a changed provider configuration cannot inherit a
    stale approval; a mismatch is rejected before credential resolution or network.
    """

    name: str
    endpoint: str
    group: str
    key_env: str
    model_ids: tuple[str, ...]


class ProviderApprovalRequired(ValueError):
    """The operator did not state the full provider facts to approve."""


class ProviderApprovalMismatch(ValueError):
    """The approved provider facts do not match the configured provider."""


def secret_values(
    secret_env_keys: frozenset[str], env_file_values: dict[str, str | None]
) -> tuple[str, ...]:
    values: list[str] = []
    for key in secret_env_keys:
        value = os.environ.get(key) or env_file_values.get(key)
        if value:
            values.append(value)
    return tuple(values)


def expected_approval(provider: object, group: object) -> ProviderApproval:
    config = provider.config  # type: ignore[attr-defined]
    return ProviderApproval(
        name=provider.name,  # type: ignore[attr-defined]
        endpoint=config.base_url,
        group=group.name,  # type: ignore[attr-defined]
        key_env=config.api_key_env,
        model_ids=tuple(group.model_ids),  # type: ignore[attr-defined]
    )


@dataclass(frozen=True)
class ApprovedProvider:
    provider: NamedProvider
    group: ModelConfig
    api_key: str | None = field(repr=False)
    secret_env_keys: frozenset[str]
    sensitive_values: tuple[str, ...] = field(repr=False)
    context_budget: ContextBudget


def load_approved_provider(
    workspace: Path,
    approval: ProviderApproval | None,
    *,
    on_secrets_resolved: Callable[[Sequence[str]], None] | None = None,
) -> ApprovedProvider:
    values = dotenv_values(workspace / ".env")
    project = load_project_providers(workspace / ".cairn" / "models.toml")
    provider = default_provider(project, os.environ, values)
    group = provider.config.model_config[0]
    if approval is None:
        raise ProviderApprovalRequired(
            "Pass --approve-provider-name, --approve-provider-endpoint, --approve-provider-group, --approve-provider-key-env and --approve-provider-models to authorize this provider."
        )
    expected = expected_approval(provider, group)
    if approval != expected:
        raise ProviderApprovalMismatch(
            f"approved provider does not match the configured provider (approved {approval}; configured {expected})"
        )
    key = resolve_provider_api_key(provider.config, os.environ, values)
    names = frozenset(
        named.config.api_key_env for named in project.providers or (provider,)
    )
    secrets = secret_values(names, values)
    if on_secrets_resolved is not None:
        on_secrets_resolved(secrets)
    budget = resolve_context_budget(
        os.environ,
        values,
        reserve_percent=getattr(project, "context_reserve_percent", 10),
    )
    return ApprovedProvider(provider, group, key, names, secrets, budget)
