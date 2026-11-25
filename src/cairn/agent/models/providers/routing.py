"""Route model IDs for OpenAI-compatible endpoints through LiteLLM's openai adapter."""


def openai_compatible_model(model: str) -> str:
    """Return the LiteLLM model ID for a configured OpenAI-compatible endpoint.

    Provider settings and ``/models`` discovery yield bare IDs. LiteLLM treats
    an unrecognized bare ID as an unsupported provider and raises ``BadRequest``
    before sending, and it would also hijack IDs that start with a native
    provider name. Every Cairn provider is OpenAI-compatible, so route every ID
    through openai/ exactly once.
    """
    return model if model.startswith("openai/") else "openai/" + model
