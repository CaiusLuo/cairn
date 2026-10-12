"""Configuration data shapes and validation.

This module contains no file I/O, environment lookup or persistent editing. It is
shared by the TOML parser, secret resolver and model-file editor.
"""

import ipaddress
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from cairn.core.model_manager import ModelConfig, ProviderConfig
from cairn.core.provider_catalog import NamedProvider, ProviderCatalog

#: Environment settings of the ``.env`` layout, named once so the loader, the
#: secret resolver and the implicit-provider builder cannot drift apart.
CAIRN_LLM_MODEL_ENV_NAME = "CAIRN_LLM_MODEL"
CAIRN_LLM_API_KEY_ENV_NAME = "CAIRN_LLM_API_KEY"
CAIRN_BASE_URL_ENV_NAME = "CAIRN_BASE_URL"

CAIRN_CONFIG_ENV_NAMES = (
    CAIRN_LLM_MODEL_ENV_NAME,
    CAIRN_LLM_API_KEY_ENV_NAME,
    CAIRN_BASE_URL_ENV_NAME,
)

#: Name of the implicit provider in the .env and legacy single-provider layouts.
DEFAULT_PROVIDER_NAME = "default"


class ConfigLayout(StrEnum):
    """Where a project's provider configuration comes from."""

    ENV = "env"
    LEGACY = "legacy"
    CATALOG = "catalog"


@dataclass(frozen=True)
class ProjectProviders:
    """Detected layout and the named providers it declares, in file order.

    ``providers`` is empty only for the ``env`` layout, whose single provider is
    built from ``CAIRN_*`` environment values.
    """

    layout: ConfigLayout
    providers: tuple[NamedProvider, ...]


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value


def parse_model_groups(model_values: object, field: str) -> tuple[ModelConfig, ...]:
    if not isinstance(model_values, list) or not model_values:
        raise ValueError(f"{field} must be a non-empty array of tables.")

    models: list[ModelConfig] = []
    names: set[str] = set()
    for index, model in enumerate(model_values):
        group = f"{field}[{index}]"
        if not isinstance(model, dict):
            raise ValueError(f"{group} must be a table.")
        name = _required_string(model.get("name"), f"{group}.name")
        if "model_id" in model:
            raise ValueError(
                f"{group}.model_id is no longer supported; remove it and "
                'use model_ids = ["provider/model"] instead.'
            )
        model_ids = model.get("model_ids")
        if not isinstance(model_ids, list) or not model_ids:
            raise ValueError(f"{group}.model_ids must be a non-empty array.")
        ids = tuple(
            _required_string(value, f"{group}.model_ids[{position}]")
            for position, value in enumerate(model_ids)
        )
        if len(set(ids)) != len(ids):
            raise ValueError(f"Duplicate model IDs in {group}.model_ids.")
        if name in names:
            raise ValueError(f"Duplicate model name: {name!r}.")
        names.add(name)
        models.append(ModelConfig(name=name, model_ids=ids))

    return tuple(models)


def parse_model_config(values: Mapping[str, object]) -> ProviderConfig:
    base_url = _required_string(values.get("base_url"), "base_url")
    api_key_env = _required_string(values.get("api_key_env"), "api_key_env")

    return ProviderConfig(
        base_url=base_url,
        api_key_env=api_key_env,
        model_config=parse_model_groups(values.get("models"), "models"),
    )


def parse_provider_catalog(values: Mapping[str, object]) -> ProviderCatalog:
    legacy_keys = [
        key for key in ("base_url", "api_key_env", "models") if key in values
    ]
    if legacy_keys:
        fields = ", ".join(legacy_keys)
        if "providers" in values:
            raise ValueError(
                "Configuration mixes the legacy single-provider layout "
                f"({fields}) with [[providers]]; keep only one layout."
            )
        raise ValueError(
            "Configuration uses the legacy single-provider layout "
            f"({fields}); load it with load_model_config() or migrate it to "
            "[[providers]]."
        )

    provider_values = values.get("providers")
    if not isinstance(provider_values, list) or not provider_values:
        raise ValueError("providers must be a non-empty array of tables.")

    providers: list[NamedProvider] = []
    names: set[str] = set()
    for index, provider in enumerate(provider_values):
        field = f"providers[{index}]"
        if not isinstance(provider, dict):
            raise ValueError(f"{field} must be a table.")
        name = _required_string(provider.get("name"), f"{field}.name")
        if not name.isprintable() or any(character.isspace() for character in name):
            raise ValueError(
                f"{field}.name must be printable and contain no whitespace."
            )
        if name in names:
            raise ValueError(f"Duplicate provider name: {name!r}.")
        names.add(name)
        config = ProviderConfig(
            base_url=_required_string(provider.get("base_url"), f"{field}.base_url"),
            api_key_env=_required_string(
                provider.get("api_key_env"), f"{field}.api_key_env"
            ),
            model_config=parse_model_groups(provider.get("models"), f"{field}.models"),
        )
        try:
            validate_runtime_provider(config)
        except ValueError as error:
            raise ValueError(f"{field}: {error}") from None
        providers.append(NamedProvider(name=name, config=config))

    return ProviderCatalog(providers=tuple(providers))


def validate_runtime_provider(config: ProviderConfig) -> None:
    """Validate project-controlled routing before asking for session approval."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config.api_key_env):
        raise ValueError("api_key_env must be an environment variable name.")
    if any(character.isspace() for character in config.base_url):
        raise ValueError("base_url must not contain whitespace.")
    url = urlsplit(config.base_url)
    if (
        not config.base_url.isprintable()
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "base_url must be an endpoint without credentials, query or fragment."
        )
    # Accessing port also rejects malformed port numbers before any credential use.
    _ = url.port
    loopback = url.hostname == "localhost"
    with suppress(ValueError):
        loopback = loopback or ipaddress.ip_address(url.hostname).is_loopback
    if url.scheme != "https" and not (url.scheme == "http" and loopback):
        raise ValueError("base_url requires HTTPS, except for loopback HTTP endpoints.")
    for model in config.model_config:
        if not model.name.isprintable() or any(c.isspace() for c in model.name):
            raise ValueError("Model names must be printable and contain no whitespace.")
        if any(not model_id.isprintable() for model_id in model.model_ids):
            raise ValueError("Model IDs must be printable.")
