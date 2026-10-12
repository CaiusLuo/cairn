"""Configuration loading and layout selection.

Parsing is deliberately separate from credential resolution and persistent edits:
loading project configuration validates project-controlled routing and never
reads or returns secret values. The ``.env`` layout's implicit provider is built
by :mod:`cairn.infra.config_secrets`, which owns environment resolution.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path

from cairn.core.model_manager import ProviderConfig
from cairn.core.provider_catalog import NamedProvider, ProviderCatalog
from cairn.infra.config_schema import (
    DEFAULT_PROVIDER_NAME,
    ConfigLayout,
    ProjectProviders,
    parse_model_config,
    parse_provider_catalog,
    validate_runtime_provider,
)
from cairn.infra.config_secrets import environment_provider


def load_project_providers(
    path: Path = Path(".cairn/models.toml"),
) -> ProjectProviders:
    """Detect and load the project's provider configuration.

    A missing ``.cairn/models.toml`` selects the ``.env`` layout. An existing
    file uses the catalog layout when it declares ``providers``; every other
    existing file must satisfy the legacy single-provider contract. A file that
    exists but cannot be read or validated always raises, so the caller never
    silently falls back to environment settings.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return ProjectProviders(layout=ConfigLayout.ENV, providers=())

    with path.open("rb") as source:
        values = tomllib.load(source)

    if "providers" in values:
        catalog = parse_provider_catalog(values)
        return ProjectProviders(
            layout=ConfigLayout.CATALOG,
            providers=catalog.providers,
        )

    provider = parse_model_config(values)
    validate_runtime_provider(provider)
    return ProjectProviders(
        layout=ConfigLayout.LEGACY,
        providers=(NamedProvider(name=DEFAULT_PROVIDER_NAME, config=provider),),
    )


def default_provider(
    project: ProjectProviders,
    host_env: Mapping[str, str],
    env_file_values: Mapping[str, str | None],
) -> NamedProvider:
    """Use the first declared provider, or the environment-only layout."""
    if project.layout is ConfigLayout.ENV:
        return environment_provider(host_env, env_file_values)
    return project.providers[0]


def load_model_config(path: Path = Path(".cairn/models.toml")) -> ProviderConfig:
    """Load a provider and its ordered model groups without resolving credentials.

    Relative paths are resolved from the caller's working directory. File and
    TOML parsing errors propagate; invalid configuration values raise ValueError.
    """
    with path.open("rb") as source:
        values = tomllib.load(source)

    return parse_model_config(values)


def load_provider_catalog(path: Path = Path(".cairn/models.toml")) -> ProviderCatalog:
    """Load every named provider without resolving credentials.

    This reads the multi-provider ``[[providers]]`` layout and is separate from
    :func:`load_model_config`, which keeps loading the legacy single-provider
    layout. Relative paths are resolved from the caller's working directory.
    File and TOML parsing errors propagate; invalid configuration values raise
    ValueError.
    """
    with path.open("rb") as source:
        values = tomllib.load(source)

    return parse_provider_catalog(values)
