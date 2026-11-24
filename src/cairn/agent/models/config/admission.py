"""Host approval before credential resolution, shared by all execution entries."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import dotenv_values

from cairn.agent.context.window import ContextBudget
from cairn.agent.models.catalog import NamedProvider
from cairn.agent.models.config import (
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.agent.models.selection import ModelConfig


@dataclass(frozen=True, slots=True)
class ApprovedGroup:
    """One model group the operator approved, with its exact ordered model ids."""

    name: str
    model_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderApproval:
    """The exact provider facts an operator approves, bound before any credential.

    Binding identity, endpoint, credential *variable name* and every approved
    model group with its full ordered model ids means a changed provider
    configuration cannot inherit a stale approval; a mismatch is rejected
    before credential resolution or network.

    ``groups`` is the ordered set available to the chat selector; the first
    entry is the default. An empty ``groups`` is a legacy approval that binds
    only ``group``/``model_ids``, which must be the provider's first group.
    """

    name: str
    endpoint: str
    group: str
    key_env: str
    model_ids: tuple[str, ...]
    groups: tuple[ApprovedGroup, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderAllowlist:
    """Every provider the host has approved, plus the legacy single-provider form.

    A version-1 approval binds one provider.  Version 2 keeps the same exact
    binding per provider but allows the runtime to select among them without
    rewriting deployment files.  The allowlist is still host policy: a provider
    present in ``models.toml`` but absent here can never be served.
    """

    providers: tuple[ProviderApproval, ...]
    version: int = 2

    def by_name(self, name: str) -> ProviderApproval | None:
        return next(
            (approval for approval in self.providers if approval.name == name),
            None,
        )


class ProviderApprovalRequired(ValueError):
    """The operator did not state the full provider facts to approve."""


class ProviderApprovalMismatch(ValueError):
    """The approved provider facts do not match the configured provider."""


_APPROVAL_FIELDS = frozenset({"name", "endpoint", "group", "key_env", "model_ids"})


def _approval_fields(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("provider approval must be a JSON object")
    fields: dict[str, object] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError("provider approval keys must be strings")
        fields[key] = value
    return fields


def _approval_text(fields: Mapping[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"provider approval {key} must be a non-empty string")
    return value


def _approval_model_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("provider approval model_ids must be a non-empty list")
    model_ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("provider approval model_ids must be non-empty strings")
        model_ids.append(item)
    return tuple(model_ids)


def _approval_groups(value: object) -> tuple[ApprovedGroup, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("provider approval groups must be a non-empty list")
    groups: list[ApprovedGroup] = []
    for entry in value:
        fields = _approval_fields(entry)
        if set(fields) != {"name", "model_ids"}:
            raise ValueError(
                "provider approval groups must contain only name and model_ids"
            )
        groups.append(
            ApprovedGroup(
                name=_approval_text(fields, "name"),
                model_ids=_approval_model_ids(fields["model_ids"]),
            )
        )
    if len({group.name for group in groups}) != len(groups):
        raise ValueError("provider approval group names must be unique")
    return tuple(groups)


def parse_provider_approval(data: object) -> ProviderApproval:
    """Parse an approval file, accepting the legacy single-group shape."""
    fields = _approval_fields(data)
    if not set(fields) >= _APPROVAL_FIELDS or set(fields) - _APPROVAL_FIELDS - {
        "groups"
    }:
        raise ValueError("provider approval has missing or unexpected fields")
    name = _approval_text(fields, "name")
    endpoint = _approval_text(fields, "endpoint")
    group = _approval_text(fields, "group")
    key_env = _approval_text(fields, "key_env")
    model_ids = _approval_model_ids(fields["model_ids"])
    groups: tuple[ApprovedGroup, ...] = ()
    if "groups" in fields:
        groups = _approval_groups(fields["groups"])
        if groups[0] != ApprovedGroup(group, model_ids):
            raise ValueError(
                "provider approval group must equal its first approved group"
            )
    return ProviderApproval(
        name=name,
        endpoint=endpoint,
        group=group,
        key_env=key_env,
        model_ids=model_ids,
        groups=groups,
    )


def parse_provider_allowlist(data: object) -> ProviderAllowlist:
    """Parse a v2 allowlist or the legacy single-provider approval.

    The legacy shape is accepted so an existing deployment can start, inspect
    its settings and migrate on the next explicit save.  It binds exactly one
    provider and therefore behaves like the previous release until then.
    """
    fields = _approval_fields(data)
    if "providers" not in fields:
        return ProviderAllowlist((parse_provider_approval(data),), version=1)
    unexpected = set(fields) - {"version", "providers"}
    if unexpected or fields.get("version") != 2:
        raise ValueError("provider allowlist has missing or unexpected fields")
    entries = fields["providers"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("provider allowlist providers must be a non-empty list")
    providers = tuple(parse_provider_approval(entry) for entry in entries)
    names = {approval.name for approval in providers}
    if len(names) != len(providers):
        raise ValueError("provider allowlist names must be unique")
    return ProviderAllowlist(providers)


def secret_values(
    secret_env_keys: frozenset[str],
    env_file_values: dict[str, str | None],
    host_env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    host = os.environ if host_env is None else host_env
    values: list[str] = []
    for key in secret_env_keys:
        value = host.get(key) or env_file_values.get(key)
        if value:
            values.append(value)
    return tuple(values)


def expected_catalog_approval(provider: object) -> ProviderApproval:
    """The approval for the provider's full ordered group catalog."""
    config = provider.config  # type: ignore[attr-defined]
    groups = tuple(config.model_config)
    if not groups:
        raise ValueError("Provider must declare at least one model group")
    first = groups[0]
    return ProviderApproval(
        name=provider.name,  # type: ignore[attr-defined]
        endpoint=config.base_url,
        group=first.name,
        key_env=config.api_key_env,
        model_ids=tuple(first.model_ids),
        groups=tuple(
            ApprovedGroup(name=group.name, model_ids=tuple(group.model_ids))
            for group in groups
        ),
    )


def expected_catalog_allowlist(
    providers: Sequence[object],
) -> ProviderAllowlist:
    """Approve every provider declared by the catalog in one explicit save."""
    if not providers:
        raise ValueError("Provider catalog must declare at least one provider")
    return ProviderAllowlist(
        tuple(expected_catalog_approval(provider) for provider in providers)
    )


def approved_groups(
    approval: ProviderApproval, provider: NamedProvider
) -> tuple[ModelConfig, ...] | None:
    """The configured groups the approval covers, in approval order.

    ``None`` means the approval does not match this provider at all. A legacy
    approval (no ``groups``) covers only the configured first group, which is
    the same boundary earlier releases enforced.
    """
    config = provider.config
    if (
        approval.name != provider.name
        or approval.endpoint != config.base_url
        or approval.key_env != config.api_key_env
    ):
        return None
    if not approval.groups:
        first = config.model_config[0]
        if approval.group != first.name or approval.model_ids != tuple(first.model_ids):
            return None
        return (first,)
    configured = {group.name: group for group in config.model_config}
    selected: list[ModelConfig] = []
    for approved in approval.groups:
        group = configured.get(approved.name)
        if group is None or tuple(group.model_ids) != approved.model_ids:
            return None
        selected.append(group)
    return tuple(selected)


def approval_matches(approval: ProviderApproval, provider: NamedProvider) -> bool:
    return approved_groups(approval, provider) is not None


def approved_groups_for(
    allowlist: ProviderAllowlist, provider: NamedProvider
) -> tuple[ModelConfig, ...] | None:
    """The groups this allowlist permits for one configured provider."""
    approval = allowlist.by_name(provider.name)
    if approval is None:
        return None
    return approved_groups(approval, provider)


@dataclass(frozen=True)
class ApprovedProvider:
    provider: NamedProvider
    group: ModelConfig
    api_key: str | None = field(repr=False)
    secret_env_keys: frozenset[str]
    sensitive_values: tuple[str, ...] = field(repr=False)
    context_budget: ContextBudget
    #: Set when the provider is approved but its credential is not configured;
    #: selecting it produces a clear runtime error instead of hiding it.
    missing_credential: str | None = None


@dataclass(frozen=True)
class ApprovedCatalog:
    """The host-approved subset of the configured provider catalog."""

    providers: tuple[ApprovedProvider, ...]
    secret_env_keys: frozenset[str]
    sensitive_values: tuple[str, ...] = field(repr=False)

    def by_name(self, name: str) -> ApprovedProvider | None:
        return next(
            (provider for provider in self.providers if provider.provider.name == name),
            None,
        )


def load_approved_provider(
    workspace: Path,
    approval: ProviderApproval | None,
    *,
    on_secrets_resolved: Callable[[Sequence[str]], None] | None = None,
    host_env: Mapping[str, str] | None = None,
) -> ApprovedProvider:
    """Load the approved provider and resolve only its credential.

    ``host_env`` lets a running process re-read a workspace credential it just
    wrote without restarting systemd: startup passes the process environment,
    while a live install passes the workspace-managed values where they exist.
    """
    host = os.environ if host_env is None else host_env
    values = dotenv_values(workspace / ".env")
    project = load_project_providers(workspace / ".cairn" / "models.toml")
    provider = default_provider(project, host, values)
    if approval is None:
        raise ProviderApprovalRequired(
            "Pass --approve-provider-name, --approve-provider-endpoint, --approve-provider-group, --approve-provider-key-env and --approve-provider-models to authorize this provider."
        )
    matched = approved_groups(approval, provider)
    if matched is None:
        raise ProviderApprovalMismatch(
            "approved provider does not match the configured provider"
        )
    group = matched[0]
    if tuple(provider.config.model_config) != matched:
        # The running runtime must never expose a group the operator did not
        # approve, even though the file may declare more.
        provider = replace(
            provider, config=replace(provider.config, model_config=matched)
        )
    key = resolve_provider_api_key(provider.config, host, values)
    names = frozenset(
        named.config.api_key_env for named in project.providers or (provider,)
    )
    secrets = secret_values(names, values, host)
    if on_secrets_resolved is not None:
        on_secrets_resolved(secrets)
    budget = resolve_context_budget(
        os.environ,
        values,
        reserve_percent=getattr(project, "context_reserve_percent", 10),
    )
    return ApprovedProvider(provider, group, key, names, secrets, budget)


def load_approved_catalog(
    workspace: Path,
    allowlist: ProviderAllowlist | None,
    *,
    on_secrets_resolved: Callable[[Sequence[str]], None] | None = None,
    host_env: Mapping[str, str] | None = None,
) -> ApprovedCatalog:
    """Load every configured provider covered by the host allowlist.

    An unapproved or mismatched provider is simply not part of the result; it
    is never served and its model groups never appear as choices.  The function
    still resolves credentials for every approved provider up front, so a
    missing credential is visible before a selection tries to use it.
    """
    host = os.environ if host_env is None else host_env
    values = dotenv_values(workspace / ".env")
    project = load_project_providers(workspace / ".cairn" / "models.toml")
    configured = (
        tuple(project.providers)
        if project.providers
        else (default_provider(project, host, values),)
    )
    if allowlist is None:
        raise ProviderApprovalRequired(
            "Pass --approve-provider-name, --approve-provider-endpoint, --approve-provider-group, --approve-provider-key-env and --approve-provider-models to authorize this provider."
        )
    budget = resolve_context_budget(
        os.environ,
        values,
        reserve_percent=getattr(project, "context_reserve_percent", 10),
    )
    approved: list[ApprovedProvider] = []
    for provider in configured:
        approval = allowlist.by_name(provider.name)
        if approval is None:
            continue
        matched = approved_groups(approval, provider)
        if matched is None:
            continue
        if tuple(provider.config.model_config) != matched:
            provider = replace(
                provider, config=replace(provider.config, model_config=matched)
            )
        try:
            key = resolve_provider_api_key(provider.config, host, values)
        except ValueError:
            # A missing key makes this provider unusable, not the whole
            # catalog.  Keep it selectable so the UI can explain the missing
            # credential instead of silently dropping the provider.
            key = None
        approved.append(
            ApprovedProvider(
                provider=provider,
                group=matched[0],
                api_key=key,
                secret_env_keys=frozenset(
                    item.config.api_key_env for item in configured
                ),
                sensitive_values=(),
                context_budget=budget,
                missing_credential=(
                    provider.config.api_key_env if key is None else None
                ),
            )
        )
    if not approved:
        raise ProviderApprovalMismatch(
            "approved provider does not match the configured provider"
        )
    if all(item.missing_credential is not None for item in approved):
        # Nothing can serve a request.  Fail startup with a clear message
        # instead of looking healthy; one usable provider is enough to start,
        # and the others stay selectable with their own error.
        raise ProviderApprovalMismatch(
            "no approved provider has a configured credential"
        )
    names = frozenset(item.config.api_key_env for item in configured)
    secrets = secret_values(names, values, host)
    if on_secrets_resolved is not None:
        on_secrets_resolved(secrets)
    return ApprovedCatalog(
        providers=tuple(replace(item, sensitive_values=secrets) for item in approved),
        secret_env_keys=names,
        sensitive_values=secrets,
    )
