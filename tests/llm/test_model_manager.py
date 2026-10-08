from cairn.llm.model_manager import ModelConfig, ModelManager, ProviderConfig


def test_select_model_updates_current_model_and_remaining_candidates() -> None:
    config = ProviderConfig(
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
    assert [model.name for model in manager.candidates()] == ["flash", "plus", "max"]

    manager.select_model("plus")

    assert manager.current_model().name == "plus"
    assert [model.name for model in manager.candidates()] == ["plus", "max"]
