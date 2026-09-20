from typing import Protocol

from cairn.core.models import LLMResponse, Message

class LLMClient(Protocol):
    def generate(
            self, 
            messages: list[Message],
            tools: list[dict[str, object]] | None = None,
        ) -> LLMResponse:
        ...
