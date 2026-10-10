import ipaddress
import os
import re
import stat
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import tomlkit
from tomlkit.items import Array, Null, Whitespace

from cairn.core.context import ContextBudget
from cairn.llm.model_manager import ModelConfig, ProviderConfig
from cairn.llm.provider_catalog import NamedProvider, ProviderCatalog
from cairn.workspace.paths import resolve_workspace_path

CAIRN_CONFIG_ENV_NAMES = ("CAIRN_LLM_MODEL", "CAIRN_LLM_API_KEY", "CAIRN_BASE_URL")

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
        catalog = _parse_provider_catalog(values)
        return ProjectProviders(
            layout=ConfigLayout.CATALOG,
            providers=catalog.providers,
        )

    provider = _parse_model_config(values)
    validate_runtime_provider(provider)
    return ProjectProviders(
        layout=ConfigLayout.LEGACY,
        providers=(NamedProvider(name=DEFAULT_PROVIDER_NAME, config=provider),),
    )


def environment_provider(
    host_env: Mapping[str, str],
    env_file_values: Mapping[str, str | None],
) -> NamedProvider:
    """Build the single provider described by ``CAIRN_*`` settings.

    This is the ``.env`` layout, used only when no ``.cairn/models.toml``
    exists. It keeps the existing environment contract, including host
    precedence over ``.env``.
    """
    values = resolve_cairn_config(host_env, env_file_values)
    model_id = values["CAIRN_LLM_MODEL"]
    return NamedProvider(
        name=DEFAULT_PROVIDER_NAME,
        config=ProviderConfig(
            base_url=values["CAIRN_BASE_URL"],
            api_key_env="CAIRN_LLM_API_KEY",
            model_config=(ModelConfig(name=model_id, model_ids=(model_id,)),),
        ),
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

    return _parse_model_config(values)


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

    return _parse_provider_catalog(values)


def _parse_provider_catalog(values: Mapping[str, object]) -> ProviderCatalog:
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
            model_config=_parse_model_groups(provider.get("models"), f"{field}.models"),
        )
        try:
            validate_runtime_provider(config)
        except ValueError as error:
            raise ValueError(f"{field}: {error}") from None
        providers.append(NamedProvider(name=name, config=config))

    return ProviderCatalog(providers=tuple(providers))


def _parse_model_config(values: Mapping[str, object]) -> ProviderConfig:
    base_url = _required_string(values.get("base_url"), "base_url")
    api_key_env = _required_string(values.get("api_key_env"), "api_key_env")

    return ProviderConfig(
        base_url=base_url,
        api_key_env=api_key_env,
        model_config=_parse_model_groups(values.get("models"), "models"),
    )


def _parse_model_groups(model_values: object, field: str) -> tuple[ModelConfig, ...]:
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


def add_model_id(group: str, model_id: str, *, provider: str | None = None) -> None:
    """Append one ID to local TOML; the approved session snapshot is untouched.

    ``provider`` names the active provider in the catalog layout and is ignored
    by the legacy single-provider layout.
    """
    _edit_model_id(group, model_id, remove=False, provider=provider)


def remove_model_id(group: str, model_id: str, *, provider: str | None = None) -> None:
    """Remove one ID from local TOML without changing the approved session."""
    _edit_model_id(group, model_id, remove=True, provider=provider)


def move_model_id(
    group: str, model_id: str, position: int, *, provider: str | None = None
) -> None:
    """Move an ID to a 1-based position in local TOML, without reloading it."""
    if type(position) is not int:
        raise ValueError("Position must be an integer.")
    _edit_model_id(group, model_id, remove=False, position=position, provider=provider)


def _move_array_id(array: Array, source: int, destination: int) -> None:
    # Keep separators/indentation in their slots, but move each original string
    # and its inline comment together. Standalone comments stay in place.
    items = [array._value[array._index_map[index]] for index in range(len(array))]
    values = [(item.value, item.comment) for item in items]
    values.insert(destination, values.pop(source))
    for index, (value, comment) in enumerate(values):
        array[index] = value
        items[index].comment = comment


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


@dataclass(frozen=True)
class _EditDocument:
    """A validated ``models.toml`` document in one of the two TOML layouts."""

    catalog: bool
    providers: tuple[NamedProvider, ...]


def _parse_edit_document(values: Mapping[str, object]) -> _EditDocument:
    """Validate a whole ``models.toml`` document before editing one group."""
    if "providers" in values:
        catalog = _parse_provider_catalog(values)
        return _EditDocument(catalog=True, providers=catalog.providers)

    config = _parse_model_config(values)
    validate_runtime_provider(config)
    return _EditDocument(
        catalog=False,
        providers=(NamedProvider(name=DEFAULT_PROVIDER_NAME, config=config),),
    )


@dataclass(frozen=True)
class _EditTarget:
    """The provider and group an edit may change, with its document paths."""

    catalog: bool
    provider_index: int
    group_index: int
    group: ModelConfig

    def model_ids_array(self, document: tomlkit.TOMLDocument) -> Array:
        """Return the live ``model_ids`` array of this group's document node."""
        # The document was validated above, so both paths hold a table array.
        if self.catalog:
            array: object = document["providers"][self.provider_index]["models"][
                self.group_index
            ]["model_ids"]
        else:
            array = document["models"][self.group_index]["model_ids"]
        if not isinstance(array, Array):
            raise ValueError("model_ids must be an array.")
        return array

    def revalidate(self, values: Mapping[str, object]) -> ProviderConfig:
        """Validate an edited document and return the edited provider's config."""
        if self.catalog:
            catalog = _parse_provider_catalog(values)
            return catalog.providers[self.provider_index].config
        config = _parse_model_config(values)
        validate_runtime_provider(config)
        return config


def _edit_target(
    document: _EditDocument, group: str, provider: str | None
) -> _EditTarget:
    """Locate the group to edit, rejecting a missing provider or group.

    In the catalog layout only the named provider is searched, so an edit can
    never touch another provider's model groups.
    """
    provider_index = 0
    if document.catalog:
        if provider is None:
            raise ValueError(
                "Editing a provider catalog requires the active provider name."
            )
        match = next(
            (
                index
                for index, named in enumerate(document.providers)
                if named.name == provider
            ),
            None,
        )
        if match is None:
            raise ValueError("That provider does not exist in .cairn/models.toml.")
        provider_index = match

    config = document.providers[provider_index].config
    group_index = next(
        (
            index
            for index, model in enumerate(config.model_config)
            if model.name == group
        ),
        None,
    )
    if group_index is None:
        raise ValueError("Model group does not exist in .cairn/models.toml.")
    return _EditTarget(
        catalog=document.catalog,
        provider_index=provider_index,
        group_index=group_index,
        group=config.model_config[group_index],
    )


def _edit_model_id(
    group: str,
    model_id: str,
    *,
    remove: bool,
    position: int | None = None,
    provider: str | None = None,
) -> None:
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
        document_layout = _parse_edit_document(tomllib.loads(text))
    except ValueError:
        # Parser diagnostics may contain local values; never echo the file.
        raise ValueError(
            "Invalid .cairn/models.toml; fix its configuration before editing models."
        ) from None

    edit_target = _edit_target(document_layout, group, provider)
    ids = edit_target.group.model_ids
    if remove or position is not None:
        if model_id not in ids:
            raise ValueError("Model ID does not exist in that group.")
        if remove and len(ids) == 1:
            raise ValueError("Cannot remove the final model ID of a group.")
    elif model_id in ids:
        raise ValueError("Model ID already exists in that group.")

    if position is not None:
        if not 1 <= position <= len(ids):
            raise ValueError(f"Position must be between 1 and {len(ids)}.")
        if ids.index(model_id) == position - 1:
            return

    document = tomlkit.parse(text)
    if position is not None and tomlkit.dumps(document) != text:
        raise ValueError("Unsupported TOML formatting; cannot safely move model IDs.")
    array = edit_target.model_ids_array(document)
    if position is not None:
        _move_array_id(array, ids.index(model_id), position - 1)
    elif remove:
        _remove_array_id(array, ids.index(model_id))
    else:
        array.append(model_id)
    edited_config = edit_target.revalidate(document.unwrap())
    updated = tomlkit.dumps(document)
    if position is not None:
        try:
            reparsed = tomlkit.parse(updated)
            old_comments = [
                item.comment.as_string() for item in array._value if item.comment
            ]
            new_array = edit_target.model_ids_array(reparsed)
            new_comments = [
                item.comment.as_string() for item in new_array._value if item.comment
            ]
            if edit_target.revalidate(reparsed.unwrap()) != edited_config or sorted(
                old_comments
            ) != sorted(new_comments):
                raise ValueError
        except ValueError:
            raise ValueError(
                "Unsupported TOML formatting; cannot safely move model IDs."
            ) from None
    edit_target.revalidate(tomllib.loads(updated))

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
