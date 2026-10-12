from typing import Any, Protocol

from cairn.core.models import LLMResponse, Message


class LLMClient(Protocol):
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...
