"""Persistent editing of .cairn/models.toml.

Edits validate the entire document before mutation, preserve comments/ordering for
the supported TOML layouts, and commit changes with an atomic same-directory
replace. Editing never resolves provider credentials or mutates an active runtime.
"""

import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import tomlkit
from tomlkit.items import Array, Null, Whitespace

from cairn.core.model_manager import ModelConfig, ProviderConfig
from cairn.core.provider_catalog import NamedProvider
from cairn.infra.config.schema import (
    DEFAULT_PROVIDER_NAME,
    _required_string,
    validate_runtime_provider,
)
from cairn.infra.config.schema import (
    parse_model_config as _parse_model_config,
)
from cairn.infra.config.schema import (
    parse_provider_catalog as _parse_provider_catalog,
)
from cairn.infra.workspace.paths import resolve_workspace_path


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
