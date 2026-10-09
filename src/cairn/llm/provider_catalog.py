from dataclasses import dataclass

from cairn.llm.model_manager import ProviderConfig


@dataclass(frozen=True)
class NamedProvider:
    """A provider configuration bound to the name that selects it."""

    name: str
    config: ProviderConfig


@dataclass(frozen=True)
class ProviderCatalog:
    """Providers in declaration order, as loaded from multi-provider TOML."""

    providers: tuple[NamedProvider, ...]
