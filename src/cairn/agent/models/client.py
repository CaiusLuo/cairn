from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from cairn.agent.models.messages import LLMResponse, LLMUsage, Message


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """Append-only fragments for one indexed call; identity precedes arguments."""

    index: int
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


@dataclass(frozen=True, slots=True)
class LLMStreamChunk:
    """Provisional response data, never executable or conversation state.

    One response choice only. After finish, only usage/empty metadata may follow;
    iterator exhaustion is required before accepting the response. Adapters must
    not report synthetic SDK finish markers or estimated/running output counts
    as provider request totals.
    """

    text: str | None = None
    tool_calls: tuple[ToolCallDelta, ...] = ()
    finish_reason: str | None = None
    usage: LLMUsage | None = None
    # Transient display only; never part of the completed answer or conversation.
    reasoning_text: str | None = None


class StreamProtocolError(ValueError):
    """Malformed/incomplete provider data; diagnostics must omit raw fragments."""


class LLMClient(Protocol):
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...


@runtime_checkable
class StreamingLLMClient(Protocol):
    """Optional capability; existing generate-only clients remain valid.

    A client may opt out per configuration with ``supports_streaming=False``.
    This optional flag is not required for existing streaming implementations.
    Consumers decide before requesting; a failed stream never retries generate.
    """

    def stream_generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[LLMStreamChunk, None]: ...


@runtime_checkable
class OutputLimitedLLMClient(Protocol):
    """Optional capability: the same client with a lower per-request output cap.

    A turn budget can be smaller than the client's configured allowance. A
    client that can honour a tighter ceiling advertises it here; callers that
    need the limit wrap the client for one request instead of mutating shared
    state. Clients without the capability still work, and their usage is still
    accounted for after the response.
    """

    def with_output_limit(self, max_output_tokens: int) -> LLMClient: ...


def with_output_limit(client: LLMClient, max_output_tokens: int | None) -> LLMClient:
    """Return ``client`` bound to ``max_output_tokens`` when it supports it."""
    if max_output_tokens is None or not isinstance(client, OutputLimitedLLMClient):
        return client
    if max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive")
    return client.with_output_limit(max_output_tokens)
