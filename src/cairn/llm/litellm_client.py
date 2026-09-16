from litellm import acompletion

from cairn.models import LLMResponse, Message

class LiteLLMClient:
    def __init__(
            self, 
            model: str, 
            api_key: str | None = None,
            api_base: str | None = None, 
        ):
        self.model = model
        self.api_key = api_key
        self.api_base = api_base

    async def generate(
            self,
            messages: list[Message]
    ) -> LLMResponse:
        
        lite_messages = [
            message.model_dump()
            for message in messages
        ]

        response = await acompletion( 
            model=self.model,
            messages=lite_messages,
            api_key=self.api_key,
            api_base=self.api_base
        )

        content = response.choices[0].message.content

        return LLMResponse(
            content=content
        )