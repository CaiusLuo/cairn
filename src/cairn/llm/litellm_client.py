import json
from typing import Any

from litellm import acompletion

from cairn.core.models import LLMResponse, Message, ToolCall


def _parse_tool_arguments(
    arguments: str | None,
    tool_name: str,
    call_id: str,
) -> dict[str, Any]:
    if not arguments:
        return {}

    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid JSON arguments for tool '{tool_name}' "
            f"(call ID '{call_id}')"
        ) from error

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Arguments for tool '{tool_name}' (call ID '{call_id}') "
            "must be a JSON object"
        )

    return parsed


class LiteLLMClient:
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base

    def _to_llm_message(
        self,
        message: Message,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }

        if message.tool_call_id is not None:
            result["tool_call_id"] = message.tool_call_id

        if message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(
                            tool_call.arguments,
                            ensure_ascii=False,
                        ),
                    },
                }
                for tool_call in message.tool_calls
            ]

        return result

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        lite_messages = [self._to_llm_message(message) for message in messages]

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
                tool_calls.append(
                    ToolCall(
                        id=call.id,
                        name=call.function.name,
                        arguments=_parse_tool_arguments(
                            call.function.arguments,
                            call.function.name,
                            call.id,
                        ),
                    )
                )

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
        )
