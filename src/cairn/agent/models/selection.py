from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class ModelRef:
    """One concrete provider/group/model choice.

    A full reference, rather than a bare model id, is what makes multi-provider
    selection unambiguous and durable: the same model id can exist behind two
    different endpoints with different credentials.
    """

    provider: str
    group: str
    model: str

    def __post_init__(self) -> None:
        if not self.provider or not self.group or not self.model:
            raise ValueError("Model reference fields must be non-empty")

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "group": self.group,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ModelRef":
        if not isinstance(value, dict):
            raise ValueError("model reference must be an object")
        fields = set(value)
        if fields != {"provider", "group", "model"}:
            raise ValueError("model reference has missing or unexpected fields")
        parts = [value[key] for key in ("provider", "group", "model")]
        if not all(isinstance(part, str) for part in parts):
            raise ValueError("model reference fields must be strings")
        return cls(*parts)


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
        self._selected_id: str | None = None

    def list_models(self) -> tuple[ModelConfig, ...]:
        return self.config.model_config

    def select_model(self, model_name: str) -> ModelConfig:
        model = self._by_name.get(model_name)
        if model is None:
            raise ValueError(f"Model '{model_name}' not found in the configuration.")
        self.model = model
        self._selected_id = model.model_ids[0]
        return model

    def resolve_model_id(self, model_id: str) -> ModelConfig:
        for model in self.config.model_config:
            if model_id in model.model_ids:
                return model
        raise ValueError(f"Model id '{model_id}' not found in the configuration.")

    def select_model_id(self, model_id: str) -> ModelConfig:
        model = self.resolve_model_id(model_id)
        self.model = model
        self._selected_id = model_id
        return model

    def current_model(self) -> ModelConfig:
        return self.model

    def current_model_id(self) -> str:
        selected = self._selected_id
        return selected if selected is not None else self.model.model_ids[0]

    def candidates(self) -> tuple[ModelConfig, ...]:
        models = self.list_models()
        start = models.index(self.model)
        return models[start:]
