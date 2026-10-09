import os
import stat
from collections.abc import Callable
from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest
import tomlkit

import cairn.config as config_module
from cairn.config import (
    add_model_id,
    load_model_config,
    load_provider_catalog,
    move_model_id,
    remove_model_id,
)

move_to_first = partial(move_model_id, position=1)

TOML = """\
# Local provider
base_url = 'https://example.test/v1' # endpoint
api_key_env = "TEST_API_KEY"
extra = { enabled = true, labels = ["one", "two"] }

[[models]] # fast group
name = 'flash'
model_ids = ["openai/a", "openai/b"] # ordered
note = "keep this"

[[models]]
name = "plus"
model_ids = ["openai/c"]

[unrelated]
timeout = 30 # keep this too
"""


def write_config(tmp_path: Path, contents: str = TOML) -> Path:
    path = tmp_path / ".cairn/models.toml"
    path.parent.mkdir(parents=True)
    path.write_bytes(contents.encode("utf-8"))
    return path


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("group", ["flash", "plus"])
def test_add_preserves_order_comments_settings_and_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newline: str, group: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_API_KEY", "fake-secret-never-written")
    credential = Mock(side_effect=AssertionError("Adding must not resolve credentials"))
    monkeypatch.setattr(config_module, "resolve_provider_api_key", credential)
    original = TOML.replace("\n", newline)
    path = write_config(tmp_path, original)
    path.chmod(0o640)
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_API_KEY=fake-dotenv-secret\n")

    add_model_id(group, "openai/new")

    old_ids = '["openai/a", "openai/b"]' if group == "flash" else '["openai/c"]'
    expected = original.replace(old_ids, old_ids[:-1] + ', "openai/new"]')
    assert path.read_bytes() == expected.encode("utf-8")
    config = load_model_config(path)
    assert [model.name for model in config.model_config] == ["flash", "plus"]
    assert config.model_config[0].model_ids == (
        ("openai/a", "openai/b", "openai/new")
        if group == "flash"
        else ("openai/a", "openai/b")
    )
    assert config.model_config[1].model_ids == (
        ("openai/c", "openai/new") if group == "plus" else ("openai/c",)
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert list(path.parent.iterdir()) == [path]
    assert "fake-secret-never-written" not in path.read_text()
    assert "fake-dotenv-secret" not in path.read_text()
    assert env_file.read_text() == "TEST_API_KEY=fake-dotenv-secret\n"
    credential.assert_not_called()


def test_add_preserves_multiline_array_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = TOML.replace(
        '["openai/a", "openai/b"]',
        '[\n  "openai/a", # first\n  "openai/b", # second\n]',
    )
    path = write_config(tmp_path, original)

    add_model_id("flash", "openai/new")

    assert path.read_text() == original.replace(
        '  "openai/b", # second\n]', '  "openai/b", # second\n  "openai/new",\n]'
    )


@pytest.mark.parametrize(
    ("group", "model_id", "error"),
    [
        ("flash", "", "non-empty"),
        ("flash", " \t", "non-empty"),
        ("flash", "openai/a", "already exists"),
        ("flash", "openai/b", "already exists"),
        ("absent", "openai/new", "does not exist"),
        ("flash", "openai/new\x1b", "printable"),
    ],
)
def test_invalid_add_never_modifies_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group: str,
    model_id: str,
    error: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)

    with pytest.raises(ValueError, match=error):
        add_model_id(group, model_id)

    assert path.read_text() == TOML
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("edit", [add_model_id, remove_model_id, move_to_first])
def test_missing_toml_does_not_migrate_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Callable[[str, str], None]
) -> None:
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("CAIRN_LLM_API_KEY=fake-secret\n")

    with pytest.raises(ValueError, match=r"Create .cairn/models.toml first"):
        edit("flash", "openai/new" if edit is add_model_id else "openai/b")

    assert env_file.read_text() == "CAIRN_LLM_API_KEY=fake-secret\n"
    assert not (tmp_path / ".cairn").exists()


@pytest.mark.parametrize(
    "contents",
    [
        b"base_url = [",
        b"\xff",
        b"",
        TOML.replace('"TEST_API_KEY"', '"fake-credential-not-an-env-name!"').encode(),
        TOML.replace('"openai/b"', '"openai/a"').encode(),
        TOML.replace('name = "plus"', "name = 'flash'").encode(),
    ],
)
@pytest.mark.parametrize("edit", [add_model_id, remove_model_id, move_to_first])
def test_invalid_original_toml_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes,
    edit: Callable[[str, str], None],
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    path.write_bytes(contents)

    with pytest.raises(ValueError, match=r"Invalid \.cairn/models\.toml") as raised:
        edit("flash", "openai/new" if edit is add_model_id else "openai/b")

    assert "fake-credential" not in str(raised.value)
    assert path.read_bytes() == contents
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("operation", ["fsync", "fchmod", "replace"])
@pytest.mark.parametrize("edit", [add_model_id, remove_model_id, move_to_first])
def test_persistence_failure_keeps_original_and_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    edit: Callable[[str, str], None],
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    monkeypatch.setattr(os, operation, Mock(side_effect=OSError("fake failure")))

    with pytest.raises(OSError, match="fake failure"):
        edit("flash", "openai/new" if edit is add_model_id else "openai/b")

    assert path.read_text() == TOML
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("edit", [add_model_id, remove_model_id, move_to_first])
def test_result_is_validated_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Callable[[str, str], None]
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    monkeypatch.setattr(tomlkit, "dumps", lambda _: TOML.replace("https://", "http://"))

    with pytest.raises(
        ValueError,
        match="Unsupported TOML formatting" if edit is move_to_first else "HTTPS",
    ):
        edit("flash", "openai/new" if edit is add_model_id else "openai/b")

    assert path.read_text() == TOML
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("destination", ["file", "dangling", "parent"])
@pytest.mark.parametrize("edit", [add_model_id, remove_model_id, move_to_first])
def test_edits_reject_symlink_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    edit: Callable[[str, str], None],
) -> None:
    monkeypatch.chdir(tmp_path)
    outside = tmp_path / "outside"
    target = write_config(outside)
    path = tmp_path / ".cairn/models.toml"
    if destination == "parent":
        path.parent.symlink_to(target.parent, target_is_directory=True)
    else:
        path.parent.mkdir()
        path.symlink_to(target if destination == "file" else outside / "missing.toml")

    with pytest.raises(ValueError, match="symlink"):
        edit("flash", "openai/new" if edit is add_model_id else "openai/b")

    assert target.read_text() == TOML
    assert not list(tmp_path.rglob(".cairn-models-*"))


@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("edit", [add_model_id, remove_model_id, move_to_first])
def test_destination_changes_before_replacement_are_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink: bool,
    edit: Callable[[str, str], None],
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    external = tmp_path / "external.toml"
    external.write_text(TOML)
    changed = TOML + "# concurrent change\n"
    original_fsync = os.fsync

    def change_destination(fd: int) -> None:
        original_fsync(fd)
        if symlink:
            path.unlink()
            path.symlink_to(external)
        else:
            path.write_text(changed)

    monkeypatch.setattr(os, "fsync", change_destination)

    with pytest.raises(ValueError, match="symlink" if symlink else "changed during"):
        edit("flash", "openai/new" if edit is add_model_id else "openai/b")

    assert external.read_text() == TOML
    assert path.read_text() == (TOML if symlink else changed)
    assert not list(path.parent.glob(".cairn-models-*"))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("model_id", ["openai/a", "openai/b", "openai/d"])
def test_remove_preserves_remaining_order_and_unrelated_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newline: str, model_id: str
) -> None:
    monkeypatch.chdir(tmp_path)
    original = TOML.replace('"openai/b"]', '"openai/b", "openai/d"]').replace(
        "\n", newline
    )
    path = write_config(tmp_path, original)
    path.chmod(0o640)
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_API_KEY=fake-file-secret\n")
    credential = Mock(side_effect=AssertionError("Must not resolve credentials"))
    monkeypatch.setattr(config_module, "resolve_provider_api_key", credential)
    remaining = [
        item for item in ("openai/a", "openai/b", "openai/d") if item != model_id
    ]

    remove_model_id("flash", model_id)

    expected = original.replace(
        '["openai/a", "openai/b", "openai/d"]',
        "[" + ", ".join(f'"{item}"' for item in remaining) + "]",
    )
    assert path.read_bytes() == expected.encode()
    assert load_model_config(path).model_config[0].model_ids == tuple(remaining)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert env_file.read_text() == "TEST_API_KEY=fake-file-secret\n"
    credential.assert_not_called()


@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_remove_keeps_inline_and_standalone_array_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: int, newline: str
) -> None:
    monkeypatch.chdir(tmp_path)
    lines = ['  "openai/a", # first', '  "openai/b", # second', '  "openai/d", # third']
    array = "[\n# before\n" + "\n".join(lines) + "\n# after\n]"
    original = TOML.replace('["openai/a", "openai/b"]', array).replace("\n", newline)
    path = write_config(tmp_path, original)
    model_id = ("openai/a", "openai/b", "openai/d")[position]

    remove_model_id("flash", model_id)

    expected = original.replace(
        lines[position], lines[position].replace(f'"{model_id}",', "")
    )
    assert path.read_bytes() == expected.encode()
    assert load_model_config(path).model_config[0].model_ids == tuple(
        item for item in ("openai/a", "openai/b", "openai/d") if item != model_id
    )


@pytest.mark.parametrize(
    ("group", "model_id", "error"),
    [
        ("absent", "openai/a", "group does not exist"),
        ("flash", "openai/absent", "ID does not exist"),
        ("plus", "openai/c", "final model ID"),
        ("flash", "", "non-empty"),
        ("flash", " \t", "non-empty"),
    ],
)
def test_remove_rejects_invalid_target_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group: str,
    model_id: str,
    error: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    with pytest.raises(ValueError, match=error):
        remove_model_id(group, model_id)
    assert path.read_text() == TOML
    assert list(path.parent.iterdir()) == [path]


def test_remove_rejects_non_regular_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".cairn/models.toml"
    path.mkdir(parents=True)
    with pytest.raises(ValueError, match="regular file"):
        remove_model_id("flash", "openai/a")
    assert path.is_dir()


@pytest.mark.parametrize("edit", [add_model_id, remove_model_id, move_to_first])
def test_parent_swap_at_replace_cannot_write_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Callable[[str, str], None]
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "models.toml"
    external.write_text(TOML)
    saved = tmp_path / "saved"
    replace = os.replace

    def swap_parent(source: str, destination: str, **kwargs: int) -> None:
        path.parent.rename(saved)
        path.parent.symlink_to(outside, target_is_directory=True)
        replace(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", swap_parent)
    edit("flash", "openai/new" if edit is add_model_id else "openai/b")
    assert external.read_text() == TOML
    assert not list(saved.glob(".cairn-models-*"))
    assert load_model_config(saved / "models.toml").model_config[0].model_ids == (
        ("openai/a",)
        if edit is remove_model_id
        else ("openai/b", "openai/a")
        if edit is move_to_first
        else ("openai/a", "openai/b", "openai/new")
    )


def test_remove_only_changes_the_named_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = TOML.replace('["openai/c"]', '["openai/a"]')
    path = write_config(tmp_path, original)
    remove_model_id("flash", "openai/a")
    assert path.read_text() == original.replace(
        '["openai/a", "openai/b"]', '["openai/b"]'
    )
    assert load_model_config(path).model_config[1].model_ids == ("openai/a",)


@pytest.mark.parametrize("source", [0, 1, 2])
@pytest.mark.parametrize("position", [1, 2, 3])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_move_preserves_other_ids_groups_and_unrelated_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: int,
    position: int,
    newline: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    tokens = ["'openai/a'", '"openai/b"', '"openai/\\u0063"']
    array = "[" + ",  ".join(tokens) + ",]"
    original = TOML.replace('["openai/a", "openai/b"]', array).replace("\n", newline)
    path = write_config(tmp_path, original)
    path.chmod(0o640)
    env_file = tmp_path / ".env"
    env_file.write_text("TEST_API_KEY=fake-secret\n")
    credential = Mock(side_effect=AssertionError("Must not resolve credentials"))
    monkeypatch.setattr(config_module, "resolve_provider_api_key", credential)
    ids = ["openai/a", "openai/b", "openai/c"]

    move_model_id("flash", ids[source], position)

    tokens.insert(position - 1, tokens.pop(source))
    ids.insert(position - 1, ids.pop(source))
    assert (
        path.read_bytes()
        == original.replace(array, "[" + ",  ".join(tokens) + ",]").encode()
    )
    assert load_model_config(path).model_config[0].model_ids == tuple(ids)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert env_file.read_text() == "TEST_API_KEY=fake-secret\n"
    credential.assert_not_called()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing_comma", ["", ","])
def test_move_keeps_inline_comments_with_ids_and_standalone_comments_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    newline: str,
    trailing_comma: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    array = (
        '[\n# before\n  "openai/a", # first\n# between\n'
        f"  'openai/b'{trailing_comma} # second\n# after\n]"
    )
    original = TOML.replace('["openai/a", "openai/b"]', array).replace("\n", newline)
    path = write_config(tmp_path, original)

    move_model_id("flash", "openai/b", 1)

    expected = original.replace('"openai/a", # first', "'openai/b', # second").replace(
        f"'openai/b'{trailing_comma} # second{newline}# after",
        f'"openai/a"{trailing_comma} # first{newline}# after',
    )
    assert path.read_bytes() == expected.encode()


@pytest.mark.parametrize(
    "group,model_id,position,error",
    [
        ("absent", "openai/a", 1, "group does not exist"),
        ("flash", "openai/c", 1, "ID does not exist"),
        ("flash", "openai/a", 0, "between 1 and 2"),
        ("flash", "openai/a", -1, "between 1 and 2"),
        ("flash", "openai/a", 3, "between 1 and 2"),
        ("flash", "", 1, "non-empty"),
    ],
)
def test_move_rejects_invalid_target_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group: str,
    model_id: str,
    position: int,
    error: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    with pytest.raises(ValueError, match=error):
        move_model_id(group, model_id, position)
    assert path.read_text() == TOML


@pytest.mark.parametrize(
    "group,model_id,position",
    [
        ("flash", "openai/a", 1),
        ("flash", "openai/b", 2),
        ("plus", "openai/c", 1),
    ],
)
def test_noop_move_never_opens_a_write_or_replaces_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group: str,
    model_id: str,
    position: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    before = path.stat()
    opened = Mock(side_effect=AssertionError("No-op must not open a write"))
    monkeypatch.setattr(os, "open", opened)
    move_model_id(group, model_id, position)
    after = path.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert path.read_text() == TOML
    opened.assert_not_called()


def test_move_rejects_comment_that_would_swallow_a_same_line_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    original = TOML.replace(
        '["openai/a", "openai/b"]',
        '["openai/a", "openai/b", # second\n "openai/d"]',
    )
    path = write_config(tmp_path, original)
    with pytest.raises(ValueError, match="Unsupported TOML formatting"):
        move_model_id("flash", "openai/b", 1)
    assert path.read_text() == original


def test_move_rejects_lossy_toml_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    dumps = tomlkit.dumps
    monkeypatch.setattr(
        tomlkit, "dumps", lambda doc: dumps(doc).replace("# ordered", "")
    )
    with pytest.raises(ValueError, match="Unsupported TOML formatting"):
        move_model_id("flash", "openai/b", 1)
    assert path.read_text() == TOML


def test_move_preserves_unrelated_nan_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    original = TOML + "value = nan # valid unrelated TOML\n"
    path = write_config(tmp_path, original)
    move_model_id("flash", "openai/b", 1)
    assert path.read_text() == original.replace(
        '["openai/a", "openai/b"]', '["openai/b", "openai/a"]'
    )


CATALOG_TOML = """\
# two providers
[[providers]]
name = "bailian"
base_url = 'https://example.test/v1' # endpoint
api_key_env = "BAILIAN_API_KEY"

[[providers.models]]
name = "flash" # fast group
model_ids = ["openai/a", "openai/b"] # ordered

[[providers.models]]
name = "plus"
model_ids = ["openai/c"]

[[providers]]
name = "local"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"

[[providers.models]]
name = "default"
model_ids = ["openai/z", "openai/y"] # local order
"""


def write_catalog_config(tmp_path: Path, contents: str = CATALOG_TOML) -> Path:
    path = tmp_path / ".cairn/models.toml"
    path.parent.mkdir(parents=True)
    path.write_bytes(contents.encode("utf-8"))
    return path


def test_catalog_add_changes_only_the_active_providers_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_catalog_config(tmp_path)

    add_model_id("flash", "openai/new", provider="bailian")

    assert path.read_text() == CATALOG_TOML.replace(
        '["openai/a", "openai/b"] # ordered',
        '["openai/a", "openai/b", "openai/new"] # ordered',
    )
    catalog = load_provider_catalog(path)
    assert catalog.providers[0].config.model_config[0].model_ids == (
        "openai/a",
        "openai/b",
        "openai/new",
    )
    assert catalog.providers[1].config.model_config[0].model_ids == (
        "openai/z",
        "openai/y",
    )


def test_catalog_remove_and_move_change_only_the_active_providers_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_catalog_config(tmp_path)

    remove_model_id("flash", "openai/a", provider="bailian")
    move_model_id("default", "openai/y", 1, provider="local")

    catalog = load_provider_catalog(path)
    assert catalog.providers[0].config.model_config[0].model_ids == ("openai/b",)
    assert catalog.providers[1].config.model_config[0].model_ids == (
        "openai/y",
        "openai/z",
    )
    assert "# two providers" in path.read_text()
    assert 'name = "flash" # fast group' in path.read_text()


@pytest.mark.parametrize(
    ("group", "model_id", "provider", "error"),
    [
        ("flash", "openai/new", None, "requires the active provider name"),
        ("flash", "openai/new", "absent", "provider does not exist"),
        ("default", "openai/new", "bailian", "group does not exist"),
        ("flash", "openai/a", "bailian", "already exists"),
    ],
)
def test_catalog_edit_rejects_an_unknown_target_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    group: str,
    model_id: str,
    provider: str | None,
    error: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_catalog_config(tmp_path)

    with pytest.raises(ValueError, match=error):
        add_model_id(group, model_id, provider=provider)

    assert path.read_text() == CATALOG_TOML


def test_catalog_remove_rejects_a_missing_id_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_catalog_config(tmp_path)

    with pytest.raises(ValueError, match="ID does not exist"):
        remove_model_id("flash", "openai/absent", provider="bailian")

    assert path.read_text() == CATALOG_TOML


def test_catalog_noop_move_never_rewrites_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_catalog_config(tmp_path)
    before = path.stat()
    opened = Mock(side_effect=AssertionError("No-op must not open a write"))
    monkeypatch.setattr(os, "open", opened)

    move_model_id("default", "openai/y", 2, provider="local")

    after = path.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    opened.assert_not_called()


@pytest.mark.parametrize(
    "contents",
    [
        CATALOG_TOML.replace("https://", "http://"),
        CATALOG_TOML.replace('name = "local"', 'name = "bailian"'),
        CATALOG_TOML.replace("openai/z", "openai/y"),
        "base_url = [",
    ],
)
def test_catalog_edit_rejects_an_invalid_document_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_catalog_config(tmp_path, contents)

    with pytest.raises(ValueError, match=r"Invalid \.cairn/models\.toml"):
        add_model_id("flash", "openai/new", provider="bailian")

    assert path.read_text() == contents


def test_catalog_edit_does_not_resolve_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BAILIAN_API_KEY", "fake-secret-never-written")
    credential = Mock(side_effect=AssertionError("Must not resolve credentials"))
    monkeypatch.setattr(config_module, "resolve_provider_api_key", credential)
    path = write_catalog_config(tmp_path)

    add_model_id("flash", "openai/new", provider="bailian")

    credential.assert_not_called()
    assert "fake-secret-never-written" not in path.read_text()


def test_legacy_edit_ignores_a_provider_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)

    add_model_id("flash", "openai/new", provider="default")

    assert load_model_config(path).model_config[0].model_ids == (
        "openai/a",
        "openai/b",
        "openai/new",
    )
