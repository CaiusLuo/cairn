import json
from litellm import acompletion

from cairn.models import LLMResponse, Message
from cairn.models import ToolCall

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
            messages: list[Message],
            tools: list[dict] | None = None,
    ) -> LLMResponse:
        
        lite_messages = [
            message.model_dump()
            for message in messages
        ]

        response = await acompletion( 
            model=self.model,
            messages=lite_messages,
            api_key=self.api_key,
            api_base=self.api_base,
            tools=tools,
        )

        message = response.choices[0].message

        tool_calls = []

        if message.tool_calls:
            for call in message.tool_calls:
                try:
                    args = json.loads(call.function.arguments) if call.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}

                tool_calls.append(
                    ToolCall(
                        id=call.id,
                        name=call.function.name,
                        arguments=args,
                    )
                )

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
        )