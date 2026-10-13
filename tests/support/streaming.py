"""Offline streaming doubles; Event barriers avoid timing-dependent tests."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Self

from cairn.core.llm import LLMStreamChunk
from cairn.core.models import LLMResponse, Message


@dataclass(frozen=True)
class StreamBarrier:
    entered: asyncio.Event
    release: asyncio.Event


class ScriptedStreamLLM:
    def __init__(
        self, *attempts: Sequence[LLMStreamChunk | StreamBarrier | Exception]
    ) -> None:
        self.attempts = list(attempts)
        self.calls: list[list[Message]] = []
        self.closed = 0
        self.cleanup_barrier: StreamBarrier | None = None

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        raise AssertionError("The optional stream capability must be used.")

    async def stream_generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        self.calls.append([message.model_copy(deep=True) for message in messages])
        script = self.attempts.pop(0)
        try:
            for item in script:
                if isinstance(item, StreamBarrier):
                    item.entered.set()
                    await item.release.wait()
                elif isinstance(item, Exception):
                    raise item
                else:
                    yield item
        finally:
            if self.cleanup_barrier is not None:
                self.cleanup_barrier.entered.set()
                await self.cleanup_barrier.release.wait()
            self.closed += 1


def provider_chunk(
    text: str | None = None,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "delta": {"content": text, "tool_calls": tool_calls},
                "finish_reason": finish,
            }
        ],
        "usage": usage,
    }


class ProviderStream:
    def __init__(self, *items: object) -> None:
        self.items = iter(items)
        self.closed = False
        self.close_barrier: StreamBarrier | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        item = next(self.items, StopAsyncIteration)
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        if isinstance(item, StreamBarrier):
            item.entered.set()
            await item.release.wait()
            return await self.__anext__()
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        if self.close_barrier is not None:
            self.close_barrier.entered.set()
            await self.close_barrier.release.wait()
        self.closed = True


class CompletionResponse(SimpleNamespace):
    """Existing completed-response fixtures also satisfy mocked stream=True.

    Keep legacy message/usage assertions and provide SDK-shaped fragments to
    exercise the same real runtime call chain now that it prefers streaming.
    """

    def __aiter__(self) -> AsyncGenerator[dict[str, Any], None]:
        return self._chunks()

    async def _chunks(self) -> AsyncGenerator[dict[str, Any], None]:
        message = self.choices[0].message
        calls = [
            {
                "index": index,
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for index, call in enumerate(message.tool_calls or ())
        ]
        yield provider_chunk(message.content, tool_calls=calls or None)
        yield provider_chunk(finish="tool_calls" if calls else "stop")
        usage = getattr(self, "usage", None)
        if usage is not None:
            yield {
                "choices": [],
                "usage": {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                },
            }

    async def aclose(self) -> None:
        pass
