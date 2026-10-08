import re
import tomllib
from pathlib import Path

import pytest

from cairn.config import load_model_config
from cairn.llm.model_manager import ModelConfig, ModelManager, ProviderConfig

PROVIDER_TOML = """\
base_url = "https://example.com/v1"
api_key_env = "BAILIAN_API_KEY"
"""

MODELS_TOML = """\
[[models]]
name = "flash"
model_id = "openai/qwen-flash"

[[models]]
name = "plus"
model_id = "openai/qwen-plus"

[[models]]
name = "max"
model_id = "openai/qwen-max"
"""


@pytest.mark.parametrize("use_default_path", [False, True])
def test_load_model_config_preserves_order_and_supports_model_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_default_path: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)
    path = tmp_path / (".cairn/models.toml" if use_default_path else "custom.toml")
    path.parent.mkdir(exist_ok=True)
    path.write_text(PROVIDER_TOML + MODELS_TOML, encoding="utf-8")

    config = load_model_config() if use_default_path else load_model_config(path)

    assert config == ProviderConfig(
        base_url="https://example.com/v1",
        api_key_env="BAILIAN_API_KEY",
        model_config=(
            ModelConfig("flash", "openai/qwen-flash"),
            ModelConfig("plus", "openai/qwen-plus"),
            ModelConfig("max", "openai/qwen-max"),
        ),
    )
    manager = ModelManager(config)
    assert manager.current_model().name == "flash"
    manager.select_model("plus")
    assert [model.name for model in manager.candidates()] == ["plus", "max"]


@pytest.mark.parametrize(
    ("contents", "field"),
    [
        ("", "base_url"),
        ('base_url = ""', "base_url"),
        ("base_url = 42", "base_url"),
        ('base_url = "https://example.com/v1"', "api_key_env"),
        (PROVIDER_TOML.replace('"BAILIAN_API_KEY"', '"  "'), "api_key_env"),
        (PROVIDER_TOML.replace('"BAILIAN_API_KEY"', "false"), "api_key_env"),
        (PROVIDER_TOML, "models"),
        (PROVIDER_TOML + "models = []", "models"),
        (PROVIDER_TOML + 'models = "flash"', "models"),
        (PROVIDER_TOML + "models = [1]", "models[0]"),
        (
            PROVIDER_TOML + '[[models]]\nmodel_id = "openai/qwen-flash"',
            "models[0].name",
        ),
        (PROVIDER_TOML + '[[models]]\nname = "flash"', "models[0].model_id"),
        (
            PROVIDER_TOML + MODELS_TOML.replace('name = "flash"', "name = []"),
            "models[0].name",
        ),
        (
            PROVIDER_TOML + MODELS_TOML.replace('"openai/qwen-flash"', '" "'),
            "models[0].model_id",
        ),
        (
            PROVIDER_TOML + MODELS_TOML.replace('name = "plus"', 'name = "flash"'),
            "Duplicate model name",
        ),
    ],
)
def test_load_model_config_rejects_invalid_values(
    tmp_path: Path, contents: str, field: str
) -> None:
    path = tmp_path / "models.toml"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(field)):
        load_model_config(path)


def test_load_model_config_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_model_config(tmp_path / "models.toml")


def test_load_model_config_reports_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text('base_url = "unterminated', encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        load_model_config(path)
