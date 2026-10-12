"""Environment and secret resolution for Cairn configuration.

Secret resolution is kept out of TOML parsing and editing so callers can validate
and approve project-controlled routing before touching credentials. The ``.env``
layout's implicit provider is built here for the same reason: it comes from the
environment rather than from a project file.
"""

from collections.abc import Mapping

from cairn.core.context import ContextBudget
from cairn.core.model_manager import ModelConfig, ProviderConfig
from cairn.core.provider_catalog import NamedProvider
from cairn.infra.config_schema import (
    CAIRN_BASE_URL_ENV_NAME,
    CAIRN_CONFIG_ENV_NAMES,
    CAIRN_LLM_API_KEY_ENV_NAME,
    CAIRN_LLM_MODEL_ENV_NAME,
    DEFAULT_PROVIDER_NAME,
)


def resolve_provider_api_key(
    config: ProviderConfig,
    host_env: Mapping[str, str],
    env_file_values: Mapping[str, str | None],
) -> str:
    """Resolve the selected credential without modifying the host environment."""
    value = host_env.get(config.api_key_env, env_file_values.get(config.api_key_env))
    if not value:
        raise ValueError(f"{config.api_key_env} environment variable is not set.")
    return value


def resolve_cairn_config(
    host_env: Mapping[str, str],
    env_file_values: Mapping[str, str | None],
) -> dict[str, str]:
    """Resolve Cairn configuration from the host environment and ``.env``.

    Host environment values take precedence over values from the project's
    ``.env`` file. This function returns only the Cairn configuration keys and
    does not modify ``os.environ``.
    """
    config: dict[str, str] = {}
    for name in CAIRN_CONFIG_ENV_NAMES:
        value = host_env.get(name, env_file_values.get(name))
        if not value:
            raise ValueError(f"{name} environment variable is not set.")
        config[name] = value
    return config


def environment_provider(
    host_env: Mapping[str, str],
    env_file_values: Mapping[str, str | None],
) -> NamedProvider:
    """Build the single provider described by ``CAIRN_*`` settings.

    This is the ``.env`` layout, used only when no ``.cairn/models.toml`` exists.
    It keeps the existing environment contract, including host precedence over
    ``.env``. Only the credential variable's *name* is bound into the provider;
    its value is resolved later, after the provider is approved.
    """
    values = resolve_cairn_config(host_env, env_file_values)
    model_id = values[CAIRN_LLM_MODEL_ENV_NAME]
    return NamedProvider(
        name=DEFAULT_PROVIDER_NAME,
        config=ProviderConfig(
            base_url=values[CAIRN_BASE_URL_ENV_NAME],
            api_key_env=CAIRN_LLM_API_KEY_ENV_NAME,
            model_config=(ModelConfig(name=model_id, model_ids=(model_id,)),),
        ),
    )


def resolve_context_budget(
    host_env: Mapping[str, str],
    env_file_values: Mapping[str, str | None],
) -> ContextBudget:
    defaults = ContextBudget()

    def integer_setting(name: str, default: int) -> int:
        value = host_env.get(name, env_file_values.get(name))
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{name} must be an integer.") from None

    return ContextBudget(
        max_tokens=integer_setting("CAIRN_CONTEXT_MAX_TOKENS", defaults.max_tokens),
        response_tokens=integer_setting(
            "CAIRN_RESPONSE_MAX_TOKENS", defaults.response_tokens
        ),
    )
