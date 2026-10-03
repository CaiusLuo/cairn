import json
from typing import Any

from litellm import acompletion

from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall


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
            f"Invalid JSON arguments for tool '{tool_name}' (call ID '{call_id}')"
        ) from error

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Arguments for tool '{tool_name}' (call ID '{call_id}') "
            "must be a JSON object"
        )

    return parsed


def to_llm_message(message: Message) -> dict[str, Any]:
    """Serialize one message exactly as it is sent to the provider.

    Shared with the token counter so a counted request and a sent request are
    the same payload.
    """
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


def _parse_usage(response: object) -> LLMUsage | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    input_tokens = getattr(usage, "prompt_tokens", None)
    output_tokens = getattr(usage, "completion_tokens", None)
    if input_tokens is None and output_tokens is None:
        return None

    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class LiteLLMClient:
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.max_output_tokens = max_output_tokens

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        lite_messages = [to_llm_message(message) for message in messages]
        completion_options: dict[str, Any] = {}
        if self.max_output_tokens is not None:
            completion_options["max_tokens"] = self.max_output_tokens

        response = await acompletion(
            model=self.model,
            messages=lite_messages,
            api_key=self.api_key,
            api_base=self.api_base,
            tools=tools,
            **completion_options,
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
            usage=_parse_usage(response),
        )
