import tomllib
from collections.abc import Mapping
from pathlib import Path

from cairn.core.context import ContextBudget
from cairn.llm.model_manager import ModelConfig, ProviderConfig

CAIRN_CONFIG_ENV_NAMES = ("CAIRN_LLM_MODEL", "CAIRN_LLM_API_KEY", "CAIRN_BASE_URL")


def load_model_config(path: Path = Path(".cairn/models.toml")) -> ProviderConfig:
    """Load a provider and its ordered models without resolving API credentials.

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
        model_id = _required_string(model.get("model_id"), f"models[{index}].model_id")
        if name in names:
            raise ValueError(f"Duplicate model name: {name!r}.")
        names.add(name)
        models.append(ModelConfig(name=name, model_id=model_id))

    return ProviderConfig(
        base_url=base_url,
        api_key_env=api_key_env,
        model_config=tuple(models),
    )


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
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
