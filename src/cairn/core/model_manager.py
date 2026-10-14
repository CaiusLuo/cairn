from dataclasses import dataclass


@dataclass(frozen=True)
class ModelConfig:
    name: str
    model_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key_env: str
    model_config: tuple[ModelConfig, ...]


class ModelManager:
    def __init__(self, config: ProviderConfig):
        if not config.model_config:
            raise ValueError("At least one model configuration must be provided.")

        names = {model.name for model in config.model_config}
        if len(names) != len(config.model_config):
            raise ValueError("Model names must be unique.")

        self.config = config
        # Names are unique, so this lookup is lossless and keeps declaration order.
        self._by_name = {model.name: model for model in config.model_config}
        self.model = config.model_config[0]

    def list_models(self) -> tuple[ModelConfig, ...]:
        return self.config.model_config

    def select_model(self, model_name: str) -> ModelConfig:
        model = self._by_name.get(model_name)
        if model is None:
            raise ValueError(f"Model '{model_name}' not found in the configuration.")
        self.model = model
        return model

    def current_model(self) -> ModelConfig:
        return self.model

    def candidates(self) -> tuple[ModelConfig, ...]:
        models = self.list_models()
        start = models.index(self.model)
        return models[start:]
