from typing import Protocol

from cairn.models import LLMResponse, Message

class LLM(Protocol):
    def generate(
            self, 
            messages: list[Message]
        ) -> LLMResponse:
        ...
