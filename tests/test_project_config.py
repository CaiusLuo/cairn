import tomllib
from pathlib import Path

import pytest

from cairn.config import (
    DEFAULT_PROVIDER_NAME,
    ConfigLayout,
    ProjectProviders,
    environment_provider,
    load_project_providers,
)
from cairn.llm.model_manager import ModelConfig, ProviderConfig
from cairn.llm.provider_catalog import NamedProvider

LEGACY_TOML = """\
base_url = "https://legacy.test/v1"
api_key_env = "LEGACY_API_KEY"

[[models]]
name = "flash"
model_ids = ["openai/model-a"]
"""

CATALOG_TOML = """\
[[providers]]
name = "bailian"
base_url = "https://bailian.test/v1"
api_key_env = "BAILIAN_API_KEY"

[[providers.models]]
name = "flash"
model_ids = ["openai/model-a", "openai/model-b"]

[[providers]]
name = "local"
base_url = "http://localhost:8000/v1"
api_key_env = "LOCAL_API_KEY"

[[providers.models]]
name = "default"
model_ids = ["openai/local-model"]
"""

ENVIRONMENT = {
    "CAIRN_LLM_MODEL": "openai/env-model",
    "CAIRN_LLM_API_KEY": "env-credential",
    "CAIRN_BASE_URL": "https://env.test/v1",
}


def write_models(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / ".cairn/models.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def test_missing_toml_selects_the_env_layout(tmp_path: Path) -> None:
    project = load_project_providers(tmp_path / "models.toml")

    assert project == ProjectProviders(layout=ConfigLayout.ENV, providers=())


def test_legacy_toml_selects_one_implicit_provider(tmp_path: Path) -> None:
    project = load_project_providers(write_models(tmp_path, LEGACY_TOML))

    assert project.layout is ConfigLayout.LEGACY
    assert project.providers == (
        NamedProvider(
            name=DEFAULT_PROVIDER_NAME,
            config=ProviderConfig(
                base_url="https://legacy.test/v1",
                api_key_env="LEGACY_API_KEY",
                model_config=(ModelConfig("flash", ("openai/model-a",)),),
            ),
        ),
    )


def test_catalog_toml_selects_every_provider_in_order(tmp_path: Path) -> None:
    project = load_project_providers(write_models(tmp_path, CATALOG_TOML))

    assert project.layout is ConfigLayout.CATALOG
    assert [provider.name for provider in project.providers] == ["bailian", "local"]
    assert project.providers[0].config.model_config == (
        ModelConfig("flash", ("openai/model-a", "openai/model-b")),
    )
    assert project.providers[1].config.base_url == "http://localhost:8000/v1"


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "base_url = [",
        LEGACY_TOML.replace("https://", "http://"),
        CATALOG_TOML.replace("https://bailian.test/v1", "http://bailian.test/v1"),
        CATALOG_TOML.replace('name = "local"', 'name = "bailian"'),
        CATALOG_TOML.replace(
            'api_key_env = "BAILIAN_API_KEY"', 'api_key_env = "NOT A NAME"'
        ),
        CATALOG_TOML.replace('model_ids = ["openai/local-model"]', "model_ids = []"),
        LEGACY_TOML + "\n" + CATALOG_TOML,
        'base_url = "https://legacy.test/v1"\n' + CATALOG_TOML,
    ],
)
def test_existing_but_invalid_toml_never_selects_another_layout(
    tmp_path: Path, contents: str
) -> None:
    path = write_models(tmp_path, contents)

    with pytest.raises((ValueError, tomllib.TOMLDecodeError)):
        load_project_providers(path)


def test_dangling_symlink_is_reported_instead_of_treated_as_missing(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".cairn/models.toml"
    path.parent.mkdir()
    path.symlink_to(tmp_path / "missing.toml")

    with pytest.raises(FileNotFoundError):
        load_project_providers(path)


def test_non_file_toml_path_is_reported(tmp_path: Path) -> None:
    path = tmp_path / ".cairn/models.toml"
    path.mkdir(parents=True)

    with pytest.raises(OSError):
        load_project_providers(path)


def test_environment_provider_keeps_the_dotenv_contract() -> None:
    provider = environment_provider(
        {"CAIRN_LLM_MODEL": "host/model"},
        {
            "CAIRN_LLM_MODEL": "file/model",
            "CAIRN_LLM_API_KEY": "file-credential",
            "CAIRN_BASE_URL": "https://file.test/v1",
        },
    )

    assert provider.name == DEFAULT_PROVIDER_NAME
    assert provider.config == ProviderConfig(
        base_url="https://file.test/v1",
        api_key_env="CAIRN_LLM_API_KEY",
        model_config=(ModelConfig("host/model", ("host/model",)),),
    )


@pytest.mark.parametrize("missing", sorted(ENVIRONMENT))
def test_environment_provider_reports_missing_settings(missing: str) -> None:
    values = dict(ENVIRONMENT)
    del values[missing]

    with pytest.raises(ValueError, match=missing):
        environment_provider(values, {})
