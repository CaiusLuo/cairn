import json
import os
from collections.abc import AsyncGenerator
from typing import Any

import litellm
from litellm import acompletion

from cairn.core.llm import LLMStreamChunk, StreamProtocolError
from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall
from cairn.infra.llm.litellm_stream import stream_completion


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

    @property
    def supports_streaming(self) -> bool:
        """Select only audited chat routes using local configuration/SDK metadata.

        Do not infer support from an endpoint or resolve providers/credentials:
        unsupported and implicit routes retain their existing generate behavior.
        """
        provider, _, model = self.model.partition("/")
        if provider not in ("openai", "azure") or not model:
            return False
        if litellm.use_litellm_proxy or os.getenv(
            "USE_LITELLM_PROXY", ""
        ).lower() not in ("", "false", "0", "no", "n", "f"):
            return False
        if provider == "openai" and litellm.route_all_chat_openai_to_responses:
            return False
        if model.startswith("responses/") or any(
            name in model for name in ("ft:babbage-002", "ft:davinci-002")
        ):
            return False
        metadata = (
            litellm.model_cost.get(self.model) or litellm.model_cost.get(model) or {}
        )
        mode: object = metadata.get("mode", "chat")
        return mode == "chat"

    def stream_generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        if not self.supports_streaming:
            raise StreamProtocolError(
                "Streaming is unsupported for this configuration."
            )
        options: dict[str, Any] = {}
        if self.max_output_tokens is not None:
            options["max_tokens"] = self.max_output_tokens
        return stream_completion(
            lambda: acompletion(
                model=self.model,
                messages=[to_llm_message(message) for message in messages],
                api_key=self.api_key,
                api_base=self.api_base,
                tools=tools,
                stream=True,
                stream_options={"include_usage": True},
                num_retries=0,
                max_retries=0,
                **options,
            ),
            self.api_key,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        lite_messages = [to_llm_message(message) for message in messages]
        completion_options: dict[str, Any] = {}
        if self.max_output_tokens is not None:
            completion_options["max_tokens"] = self.max_output_tokens

        try:
            response = await acompletion(
                model=self.model,
                messages=lite_messages,
                api_key=self.api_key,
                api_base=self.api_base,
                tools=tools,
                **completion_options,
            )
        except Exception as exc:
            # Provider errors reach both trace spans and terminal diagnostics.
            # Never propagate an error that echoes the selected credential.
            if self.api_key and self.api_key in str(exc):
                raise RuntimeError(
                    "LLM request failed; credential-bearing details were omitted."
                ) from None
            raise

        message = response.choices[0].message

        tool_calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=_parse_tool_arguments(
                    call.function.arguments,
                    call.function.name,
                    call.id,
                ),
            )
            for call in message.tool_calls or ()
        ]

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            usage=_parse_usage(response),
        )
