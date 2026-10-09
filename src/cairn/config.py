import ipaddress
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import tomlkit
from tomlkit.items import Array, Null, Whitespace

from cairn.core.context import ContextBudget
from cairn.llm.model_manager import ModelConfig, ProviderConfig
from cairn.workspace.paths import resolve_workspace_path

CAIRN_CONFIG_ENV_NAMES = ("CAIRN_LLM_MODEL", "CAIRN_LLM_API_KEY", "CAIRN_BASE_URL")


def load_model_config(path: Path = Path(".cairn/models.toml")) -> ProviderConfig:
    """Load a provider and its ordered model groups without resolving credentials.

    Relative paths are resolved from the caller's working directory. File and
    TOML parsing errors propagate; invalid configuration values raise ValueError.
    """
    with path.open("rb") as source:
        values = tomllib.load(source)

    return _parse_model_config(values)


def _parse_model_config(values: Mapping[str, object]) -> ProviderConfig:
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


def add_model_id(group: str, model_id: str) -> None:
    """Append one ID to local TOML; the approved session snapshot is untouched."""
    _edit_model_id(group, model_id, remove=False)


def remove_model_id(group: str, model_id: str) -> None:
    """Remove one ID from local TOML without changing the approved session."""
    _edit_model_id(group, model_id, remove=True)


def _remove_array_id(array: Array, position: int) -> None:
    # tomlkit deletion drops an element's inline comment. Keep that comment as a
    # standalone array entry instead. These internals have no public equivalent.
    item = array._value[array._index_map[position]]
    if item.comment is None:
        del array[position]
        return
    item.value = Null()
    item.comma = None
    if item.indent is not None:
        item.indent = Whitespace(item.indent.s.replace(",", ""))
    list.__delitem__(array, position)
    array._reindex()


def _edit_model_id(group: str, model_id: str, *, remove: bool) -> None:
    _required_string(model_id, "model_id")
    root = Path.cwd()
    relative = ".cairn/models.toml"
    path = resolve_workspace_path(root, relative)
    try:
        original_stat = path.lstat()
    except FileNotFoundError:
        raise ValueError(
            "Create .cairn/models.toml first; legacy .env settings are not migrated."
        ) from None
    if not stat.S_ISREG(original_stat.st_mode):
        raise ValueError(".cairn/models.toml must be a regular file.")
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
        config = _parse_model_config(tomllib.loads(text))
        validate_runtime_provider(config)
    except ValueError:
        # Parser diagnostics may contain local values; never echo the file.
        raise ValueError(
            "Invalid .cairn/models.toml; fix its configuration before editing models."
        ) from None

    index = next(
        (
            index
            for index, model in enumerate(config.model_config)
            if model.name == group
        ),
        None,
    )
    if index is None:
        raise ValueError("Model group does not exist in .cairn/models.toml.")
    ids = config.model_config[index].model_ids
    if remove:
        if model_id not in ids:
            raise ValueError("Model ID does not exist in that group.")
        if len(ids) == 1:
            raise ValueError("Cannot remove the final model ID of a group.")
    elif model_id in ids:
        raise ValueError("Model ID already exists in that group.")

    document = tomlkit.parse(text)
    array = document["models"][index]["model_ids"]
    if remove:
        _remove_array_id(array, ids.index(model_id))
    else:
        array.append(model_id)
    validate_runtime_provider(_parse_model_config(document.unwrap()))
    updated = tomlkit.dumps(document)
    validate_runtime_provider(_parse_model_config(tomllib.loads(updated)))

    # Anchor all writes to the checked directory, even if its pathname changes.
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".cairn-models-{uuid4().hex}"
    created = False
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        created = True
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as target:
            target.write(updated)
            target.flush()
            os.fchmod(target.fileno(), stat.S_IMODE(original_stat.st_mode))
            os.fsync(target.fileno())
        resolve_workspace_path(root, relative)
        if path.parent.stat().st_ino != os.fstat(directory).st_ino:
            raise ValueError("models.toml directory changed during the edit; retry.")
        if path.read_bytes() != original:
            raise ValueError("models.toml changed during the edit; retry the command.")
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory)


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
