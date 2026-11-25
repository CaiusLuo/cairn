"""LiteLLM routing for model IDs that name models on an OpenAI-compatible endpoint."""

import litellm

_KNOWN_PROVIDER_PREFIXES = frozenset(
    str(getattr(provider, "value", provider)) for provider in litellm.provider_list
)


def openai_compatible_model(model: str) -> str:
    """Route a model ID through LiteLLM's openai adapter.

    Provider settings and ``/models`` discovery yield bare IDs. LiteLLM treats
    an unrecognized bare ID as an unsupported provider and raises ``BadRequest``
    before any request is sent. Every Cairn provider is OpenAI-compatible, so a
    bare or org-qualified ID belongs on the openai route; an explicit known
    provider prefix is preserved.
    """
    provider, separator, _ = model.partition("/")
    if separator and provider in _KNOWN_PROVIDER_PREFIXES:
        return model
    return f"openai/{model}"
