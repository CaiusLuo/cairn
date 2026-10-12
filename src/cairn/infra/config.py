"""Public configuration facade.

Configuration responsibilities live in focused modules:

* ``config_schema`` — layout models and project-controlled validation;
* ``config_loader`` — TOML parsing and layout detection;
* ``config_secrets`` — environment/secret and context-budget resolution;
* ``config_editor`` — transactional ``.cairn/models.toml`` edits.

This module remains the stable import surface for CLI commands, examples and
existing tests.
"""

from cairn.infra.config_editor import add_model_id, move_model_id, remove_model_id
from cairn.infra.config_loader import (
    default_provider,
    environment_provider,
    load_model_config,
    load_project_providers,
    load_provider_catalog,
)
from cairn.infra.config_schema import (
    CAIRN_CONFIG_ENV_NAMES,
    DEFAULT_PROVIDER_NAME,
    ConfigLayout,
    ProjectProviders,
    validate_runtime_provider,
)
from cairn.infra.config_secrets import (
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
