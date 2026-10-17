"""Public configuration facade.

Configuration responsibilities live in focused modules:

* ``schema`` — layout models and project-controlled validation;
* ``loader`` — TOML parsing and layout detection;
* ``secrets`` — environment/secret and context-budget resolution,
  including the ``.env`` layout's implicit provider;
* ``editor`` — transactional ``.cairn/models.toml`` edits.

This package remains the stable import surface for CLI commands, examples and
existing tests.
"""

from cairn.infra.config.editor import add_model_id, move_model_id, remove_model_id
from cairn.infra.config.loader import (
    default_provider,
    load_model_config,
    load_project_providers,
    load_provider_catalog,
)
from cairn.infra.config.schema import (
    CAIRN_CONFIG_ENV_NAMES,
    DEFAULT_PROVIDER_NAME,
    ConfigLayout,
    ProjectProviders,
    validate_runtime_provider,
)
from cairn.infra.config.secrets import (
    environment_provider,
    resolve_cairn_config,
    resolve_context_budget,
    resolve_provider_api_key,
)

__all__ = [
    "CAIRN_CONFIG_ENV_NAMES",
    "DEFAULT_PROVIDER_NAME",
    "ConfigLayout",
    "ProjectProviders",
    "add_model_id",
    "default_provider",
    "environment_provider",
    "load_model_config",
    "load_project_providers",
    "load_provider_catalog",
    "move_model_id",
    "remove_model_id",
    "resolve_cairn_config",
    "resolve_context_budget",
    "resolve_provider_api_key",
    "validate_runtime_provider",
]
