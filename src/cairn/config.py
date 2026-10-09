import ipaddress
import re
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from cairn.core.context import ContextBudget
from cairn.llm.model_manager import ModelConfig, ProviderConfig

CAIRN_CONFIG_ENV_NAMES = ("CAIRN_LLM_MODEL", "CAIRN_LLM_API_KEY", "CAIRN_BASE_URL")


def load_model_config(path: Path = Path(".cairn/models.toml")) -> ProviderConfig:
    """Load a provider and its ordered model groups without resolving credentials.

    Relative paths are resolved from the caller's working directory. File and
    TOML parsing errors propagate; invalid configuration values raise ValueError.
    """
    with path.open("rb") as source:
        values = tomllib.load(source)

    base_url = _required_string(values.get("base_url"), "base_url")
    api_key_env = _required_string(values.get("api_key_env"), "api_key_env")
    model_values = values.get("models")
    if not isinstance(model_values, list) or not model_values:
        raise ValueError("models must be a non-empty array of tables.")

    models: list[ModelConfig] = []
    names: set[str] = set()
    for index, model in enumerate(model_values):
        if not isinstance(model, dict):
            raise ValueError(f"models[{index}] must be a table.")
        name = _required_string(model.get("name"), f"models[{index}].name")
        if "model_id" in model:
            raise ValueError(
                f"models[{index}].model_id is no longer supported; remove it and "
                'use model_ids = ["provider/model"] instead.'
            )
        model_ids = model.get("model_ids")
        if not isinstance(model_ids, list) or not model_ids:
            raise ValueError(f"models[{index}].model_ids must be a non-empty array.")
        ids = tuple(
            _required_string(value, f"models[{index}].model_ids[{position}]")
            for position, value in enumerate(model_ids)
        )
        if len(set(ids)) != len(ids):
            raise ValueError(f"Duplicate model IDs in models[{index}].model_ids.")
        if name in names:
            raise ValueError(f"Duplicate model name: {name!r}.")
        names.add(name)
        models.append(ModelConfig(name=name, model_ids=ids))

    return ProviderConfig(
        base_url=base_url,
        api_key_env=api_key_env,
        model_config=tuple(models),
    )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value


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
