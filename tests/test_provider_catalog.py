import dataclasses
import json
import re
import tomllib
from pathlib import Path

import pytest

from cairn.config import load_model_config, load_provider_catalog
from cairn.llm.model_manager import ModelConfig, ProviderConfig
from cairn.llm.provider_catalog import NamedProvider, ProviderCatalog

CATALOG_TOML = """\
[[providers]]
name = "bailian"
base_url = "https://example.com/v1"
api_key_env = "BAILIAN_API_KEY"

[[providers.models]]
name = "flash"
model_ids = ["openai/model-a", "openai/model-b"]

[[providers.models]]
name = "plus"
model_ids = ["openai/model-c"]

[[providers]]
name = "local"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"

[[providers.models]]
name = "default"
model_ids = ["openai/local-model"]
"""

PROVIDER_TOML = """\
[[providers]]
name = "bailian"
base_url = "https://example.com/v1"
api_key_env = "KEY"
"""

GROUP_TOML = """\
[[providers.models]]
name = "flash"
model_ids = ["openai/model-a"]
"""

LEGACY_TOML = """\
base_url = "https://example.com/v1"
api_key_env = "BAILIAN_API_KEY"

[[models]]
name = "flash"
model_ids = ["openai/model-a"]
"""


def write_config(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(contents, encoding="utf-8")
    return path


def test_load_provider_catalog_preserves_declared_order(tmp_path: Path) -> None:
    catalog = load_provider_catalog(write_config(tmp_path, CATALOG_TOML))

    assert catalog == ProviderCatalog(
        providers=(
            NamedProvider(
                name="bailian",
                config=ProviderConfig(
                    base_url="https://example.com/v1",
                    api_key_env="BAILIAN_API_KEY",
                    model_config=(
                        ModelConfig("flash", ("openai/model-a", "openai/model-b")),
                        ModelConfig("plus", ("openai/model-c",)),
                    ),
                ),
            ),
            NamedProvider(
                name="local",
                config=ProviderConfig(
                    base_url="http://localhost:8000/v1",
                    api_key_env="LOCAL_API_KEY",
                    model_config=(ModelConfig("default", ("openai/local-model",)),),
                ),
            ),
        )
    )
    assert [provider.name for provider in catalog.providers] == ["bailian", "local"]
    assert [model.name for model in catalog.providers[0].config.model_config] == [
        "flash",
        "plus",
    ]


def test_load_provider_catalog_supports_the_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".cairn/models.toml"
    path.parent.mkdir()
    path.write_text(CATALOG_TOML, encoding="utf-8")

    catalog = load_provider_catalog()

    assert [provider.name for provider in catalog.providers] == ["bailian", "local"]


def test_catalog_types_are_immutable(tmp_path: Path) -> None:
    catalog = load_provider_catalog(write_config(tmp_path, CATALOG_TOML))

    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.providers[0].name = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.providers = ()  # type: ignore[misc]


def test_load_provider_catalog_does_not_resolve_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_API_KEY", raising=False)

    catalog = load_provider_catalog(write_config(tmp_path, CATALOG_TOML))

    assert [provider.config.api_key_env for provider in catalog.providers] == [
        "BAILIAN_API_KEY",
        "LOCAL_API_KEY",
    ]


def test_load_provider_catalog_allows_repeated_groups_across_providers(
    tmp_path: Path,
) -> None:
    contents = (
        PROVIDER_TOML
        + GROUP_TOML
        + PROVIDER_TOML.replace("bailian", "local")
        + GROUP_TOML
    )

    catalog = load_provider_catalog(write_config(tmp_path, contents))

    assert [provider.name for provider in catalog.providers] == ["bailian", "local"]
    assert (
        catalog.providers[0].config.model_config
        == catalog.providers[1].config.model_config
    )


@pytest.mark.parametrize(
    ("contents", "field"),
    [
        ("", "providers"),
        ("providers = []", "providers"),
        ('providers = "bailian"', "providers"),
        ("[providers]\nname = 'bailian'", "providers"),
        ("providers = [1]", "providers[0]"),
        ('[[providers]]\nbase_url = "https://example.com/v1"', "providers[0].name"),
        (
            '[[providers]]\nname = ""\nbase_url = "https://example.com/v1"',
            "providers[0].name",
        ),
        (
            '[[providers]]\nname = []\nbase_url = "https://example.com/v1"',
            "providers[0].name",
        ),
        (
            '[[providers]]\nname = "two words"\nbase_url = "https://example.com/v1"',
            "providers[0].name",
        ),
        ('[[providers]]\nname = "bailian"', "providers[0].base_url"),
        (
            PROVIDER_TOML.replace('"https://example.com/v1"', '""'),
            "providers[0].base_url",
        ),
        (
            '[[providers]]\nname = "bailian"\nbase_url = "https://example.com/v1"',
            "providers[0].api_key_env",
        ),
        (PROVIDER_TOML, "providers[0].models"),
        (PROVIDER_TOML + "models = []", "providers[0].models"),
        (PROVIDER_TOML + 'models = "flash"', "providers[0].models"),
        (PROVIDER_TOML + '[providers.models]\nname = "flash"', "providers[0].models"),
        (PROVIDER_TOML + "models = [1]", "providers[0].models[0]"),
        (
            PROVIDER_TOML + '[[providers.models]]\nmodel_id = "openai/model-a"',
            "providers[0].models[0].name",
        ),
        (
            PROVIDER_TOML + '[[providers.models]]\nname = "flash"',
            "providers[0].models[0].model_ids",
        ),
        (
            PROVIDER_TOML + GROUP_TOML.replace('name = "flash"', "name = []"),
            "providers[0].models[0].name",
        ),
        (
            PROVIDER_TOML + GROUP_TOML.replace('"openai/model-a"', '" "'),
            "providers[0].models[0].model_ids",
        ),
        (
            PROVIDER_TOML + '[[providers.models]]\nname = "flash"\nmodel_ids = []',
            "providers[0].models[0].model_ids",
        ),
        (
            PROVIDER_TOML + '[[providers.models]]\nname = "flash"\nmodel_ids = [""]',
            "providers[0].models[0].model_ids",
        ),
        (
            PROVIDER_TOML + '[[providers.models]]\nname = "flash"\nmodel_ids = [42]',
            "providers[0].models[0].model_ids",
        ),
    ],
)
def test_load_provider_catalog_rejects_invalid_values(
    tmp_path: Path, contents: str, field: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(field)):
        load_provider_catalog(write_config(tmp_path, contents))


def test_load_provider_catalog_rejects_duplicate_provider_names(tmp_path: Path) -> None:
    contents = PROVIDER_TOML + GROUP_TOML + PROVIDER_TOML + GROUP_TOML

    with pytest.raises(ValueError, match="Duplicate provider name"):
        load_provider_catalog(write_config(tmp_path, contents))


def test_load_provider_catalog_rejects_duplicate_model_names(tmp_path: Path) -> None:
    contents = PROVIDER_TOML + GROUP_TOML + GROUP_TOML

    with pytest.raises(ValueError, match="Duplicate model name"):
        load_provider_catalog(write_config(tmp_path, contents))


def test_load_provider_catalog_rejects_duplicate_model_ids(tmp_path: Path) -> None:
    contents = PROVIDER_TOML + GROUP_TOML.replace(
        '"openai/model-a"', '"openai/model-a", "openai/model-a"'
    )

    with pytest.raises(ValueError, match="Duplicate model IDs"):
        load_provider_catalog(write_config(tmp_path, contents))


def test_load_provider_catalog_rejects_the_legacy_model_id_field(
    tmp_path: Path,
) -> None:
    contents = PROVIDER_TOML + (
        '[[providers.models]]\nname = "flash"\n'
        'model_id = "openai/model-a"\nmodel_ids = ["openai/model-b"]\n'
    )

    with pytest.raises(
        ValueError, match=r"model_id is no longer supported;.*model_ids = \["
    ):
        load_provider_catalog(write_config(tmp_path, contents))


@pytest.mark.parametrize(
    ("base_url", "api_key_env", "error"),
    [
        ("http://remote.test/v1", "KEY", "HTTPS"),
        ("file:///tmp/key", "KEY", "endpoint"),
        ("https://user:pass@example.test", "KEY", "credentials"),
        ("https://example.test/?key=value", "KEY", "query"),
        ("https://example.test/#fragment", "KEY", "fragment"),
        ("https://example.test:bad", "KEY", "Port"),
        ("https://example.test/\n", "KEY", "whitespace"),
        ("https://example.test/\x1b", "KEY", "endpoint"),
        ("https://example.test", "KEY NAME", "environment variable name"),
    ],
)
def test_load_provider_catalog_rejects_unusable_provider_routing(
    tmp_path: Path, base_url: str, api_key_env: str, error: str
) -> None:
    contents = (
        "[[providers]]\n"
        'name = "bailian"\n'
        f"base_url = {json.dumps(base_url)}\n"
        f"api_key_env = {json.dumps(api_key_env)}\n\n" + GROUP_TOML
    )

    with pytest.raises(ValueError, match=error):
        load_provider_catalog(write_config(tmp_path, contents))


def test_load_provider_catalog_reports_the_offending_provider(tmp_path: Path) -> None:
    contents = (
        PROVIDER_TOML
        + GROUP_TOML
        + '[[providers]]\nname = "remote"\nbase_url = "http://remote.test/v1"\n'
        'api_key_env = "KEY"\n\n' + GROUP_TOML
    )

    with pytest.raises(ValueError, match=r"providers\[1\]: .*HTTPS"):
        load_provider_catalog(write_config(tmp_path, contents))


def test_load_provider_catalog_rejects_unsafe_model_names_and_ids(
    tmp_path: Path,
) -> None:
    unsafe_name = PROVIDER_TOML + GROUP_TOML.replace(
        'name = "flash"', 'name = "two words"'
    )
    unsafe_id = PROVIDER_TOML + GROUP_TOML.replace(
        '"openai/model-a"', json.dumps("openai/model\x1b")
    )

    with pytest.raises(ValueError, match="Model names"):
        load_provider_catalog(write_config(tmp_path, unsafe_name))
    with pytest.raises(ValueError, match="Model IDs"):
        load_provider_catalog(write_config(tmp_path, unsafe_id))


@pytest.mark.parametrize(
    "legacy",
    [
        'base_url = "https://example.com/v1"\n',
        'api_key_env = "KEY"\n',
        "models = []\n",
    ],
)
def test_load_provider_catalog_rejects_legacy_keys_before_providers(
    tmp_path: Path, legacy: str
) -> None:
    with pytest.raises(ValueError, match="mixes the legacy single-provider layout"):
        load_provider_catalog(write_config(tmp_path, legacy + CATALOG_TOML))


def test_load_provider_catalog_rejects_legacy_tables_after_providers(
    tmp_path: Path,
) -> None:
    contents = CATALOG_TOML + (
        '[[models]]\nname = "flash"\nmodel_ids = ["openai/model-a"]\n'
    )

    with pytest.raises(ValueError, match="mixes the legacy single-provider layout"):
        load_provider_catalog(write_config(tmp_path, contents))


def test_load_provider_catalog_rejects_a_legacy_only_layout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="uses the legacy single-provider layout"):
        load_provider_catalog(write_config(tmp_path, LEGACY_TOML))


def test_load_provider_catalog_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_provider_catalog(tmp_path / "models.toml")


def test_load_provider_catalog_reports_invalid_toml(tmp_path: Path) -> None:
    path = write_config(tmp_path, '[[providers]\nname = "bailian"')

    with pytest.raises(tomllib.TOMLDecodeError):
        load_provider_catalog(path)


def test_load_model_config_keeps_returning_the_legacy_provider_type(
    tmp_path: Path,
) -> None:
    config = load_model_config(write_config(tmp_path, LEGACY_TOML))

    assert type(config) is ProviderConfig
    assert config == ProviderConfig(
        base_url="https://example.com/v1",
        api_key_env="BAILIAN_API_KEY",
        model_config=(ModelConfig("flash", ("openai/model-a",)),),
    )
