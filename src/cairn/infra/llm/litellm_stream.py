"""LiteLLM chat-stream normalization and owned provider-resource cleanup.

1.101.0 synthesizes a terminal chunk and usage at EOF. Observe its upstream
OpenAI-compatible chunks before that transformation, while still driving the SDK
wrapper to exhaustion. Synthetic output never becomes a Cairn completion fact.
"""

import asyncio
import inspect
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine
from typing import Any, Self

from litellm.exceptions import MidStreamFallbackError
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from openai import APIError as ProviderAPIError

from cairn.core.llm import LLMStreamChunk, StreamProtocolError, ToolCallDelta
from cairn.core.models import LLMUsage
from cairn.infra.llm.errors import is_fallback_error


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise StreamProtocolError("Invalid provider text fragment.")
    return value


def _usage(value: object) -> LLMUsage | None:
    if value is None:
        return None
    counters = [_field(value, name) for name in ("prompt_tokens", "completion_tokens")]
    if any(
        item is not None and (type(item) is not int or item < 0) for item in counters
    ):
        raise StreamProtocolError("Invalid provider usage counters.")
    if all(item is None for item in counters):
        return None
    return LLMUsage(input_tokens=counters[0], output_tokens=counters[1])


def normalize_chunk(raw: object) -> LLMStreamChunk:
    """One OpenAI-compatible chat choice, or a trailing usage-only chunk."""
    choices = _field(raw, "choices")
    if not isinstance(choices, list) or len(choices) > 1:
        raise StreamProtocolError("Streaming requires one chat response choice.")
    usage = _usage(_field(raw, "usage"))
    if not choices:
        if usage is None:
            raise StreamProtocolError("Empty provider stream chunk.")
        return LLMStreamChunk(usage=usage)
    choice = choices[0]
    if type(_field(choice, "index")) is not int or _field(choice, "index") != 0:
        raise StreamProtocolError("Unsupported provider response choice index.")
    delta = _field(choice, "delta")
    if delta is None:
        raise StreamProtocolError("Missing provider response delta.")
    if _field(delta, "role") not in (None, "assistant"):
        raise StreamProtocolError("Invalid provider response role.")
    # No legacy function calls, audio, refusal or reasoning-only response shape.
    if any(
        _field(delta, key)
        for key in (
            "function_call",
            "audio",
            "refusal",
            "reasoning_content",
            "reasoning",
        )
    ):
        raise StreamProtocolError("Unsupported provider response delta shape.")
    calls = _field(delta, "tool_calls")
    if calls is not None and not isinstance(calls, list):
        raise StreamProtocolError("Invalid provider tool fragment group.")
    if calls is not None and len(calls) > 32:
        # Bound normalization allocation before the runtime's cumulative limit.
        raise StreamProtocolError("Provider tool fragment group limit exceeded.")
    fragments: list[ToolCallDelta] = []
    for call in calls or ():
        index = _field(call, "index")
        if (
            type(index) is not int
            or index < 0
            or _field(call, "type") not in (None, "function")
        ):
            raise StreamProtocolError("Invalid provider tool fragment identity.")
        function = _field(call, "function")
        if function is None:
            raise StreamProtocolError("Missing provider tool function fragment.")
        fragments.append(
            ToolCallDelta(
                index=index,
                id=_text(_field(call, "id")),
                name=_text(_field(function, "name")),
                arguments=_text(_field(function, "arguments")),
            )
        )
    return LLMStreamChunk(
        text=_text(_field(delta, "content")),
        tool_calls=tuple(fragments),
        finish_reason=_text(_field(choice, "finish_reason")),
        usage=usage,
    )


async def _close(resource: Any) -> None:
    close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if close is None:
        raise StreamProtocolError("Provider stream has no close operation.")
    result = close()
    if inspect.isawaitable(result):
        await result


async def _cleanup(
    operation: Coroutine[Any, Any, None],
) -> asyncio.CancelledError | None:
    """Infrastructure cannot depend on runtime.lifecycle; finish, never detach."""
    task = asyncio.create_task(operation)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except Exception:
            break
    try:
        task.result()
    except BaseException:
        if cancelled is not None:
            cancelled.add_note("Provider cleanup failed.")
            raise cancelled from None
        raise
    return cancelled


def _safe_error(error: Exception, api_key: str | None, provisional: bool) -> Exception:
    if api_key and api_key in str(error):
        return RuntimeError(
            "LLM stream failed; credential-bearing details were omitted."
        )
    if (
        isinstance(error, MidStreamFallbackError)
        and error.original_exception is not None
    ):
        error = error.original_exception
        if api_key and api_key in str(error):
            return RuntimeError(
                "LLM stream failed; credential-bearing details were omitted."
            )
    if not provisional or isinstance(error, StreamProtocolError):
        return error
    if isinstance(error, ProviderAPIError):
        # Keep the typed SDK failure and structured quota chain for the existing
        # runtime policy. Only the legacy message-only free-tier 403 needs a safe
        # canonical signature; no provider message or generated content survives.
        free_tier = getattr(error, "status_code", None) == 403 and is_fallback_error(
            error
        )
        diagnostic = "LLM provider streaming failed."
        if free_tier:
            diagnostic += " Free quota exhausted; use free tier only."
        error.message = diagnostic
        error.args = (diagnostic,)
        return error
    return RuntimeError(f"LLM provider streaming failed ({type(error).__name__}).")


class _ProviderChunks:
    """Observe provider facts before the SDK can repair or estimate them."""

    def __init__(self, source: Any) -> None:
        if not hasattr(source, "__aiter__"):
            raise StreamProtocolError("Unsupported synchronous provider stream.")
        self.source = source
        self.iterator: AsyncIterator[Any] = source.__aiter__()
        self.pending: deque[LLMStreamChunk] = deque()
        self.count = 0
        self.close_failed = False
        self.exhausted = False
        self.failure: BaseException | None = None

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        try:
            raw = await self.iterator.__anext__()
            self.count += 1
            # Bounds even metadata the SDK discards internally before yielding.
            if self.count > 16_384:
                raise StreamProtocolError("Provider stream chunk limit exceeded.")
            self.pending.append(normalize_chunk(raw))
            return raw
        except StopAsyncIteration:
            self.exhausted = True
            raise
        except BaseException as error:
            self.failure = error
            raise

    async def aclose(self) -> None:
        try:
            try:
                if self.iterator is not self.source and (
                    hasattr(self.iterator, "aclose") or hasattr(self.iterator, "close")
                ):
                    await _close(self.iterator)
            finally:
                await _close(self.source)
        except BaseException:
            self.close_failed = True
            raise


async def stream_completion(
    open_stream: Callable[[], Coroutine[Any, Any, Any]],
    api_key: str | None,
) -> AsyncGenerator[LLMStreamChunk, None]:
    stream: Any = None
    observed: _ProviderChunks | None = None
    primary: BaseException | None = None
    try:
        # Shield acquisition so repeated caller cancellation cannot orphan a
        # stream returned by a provider that cooperatively settles its request.
        opening = asyncio.create_task(open_stream())
        try:
            stream = await asyncio.shield(opening)
        except asyncio.CancelledError as cancelled:
            opening.cancel()

            async def settle_open() -> None:
                try:
                    resource = await opening
                except BaseException:
                    return
                await _close(resource)

            try:
                await _cleanup(settle_open())
            except BaseException:
                cancelled.add_note("Cancelled provider acquisition cleanup failed.")
            raise

        if isinstance(stream, CustomStreamWrapper):
            # Native non-chat provider iterators have different completion/usage
            # semantics and need an audited normalizer before enabling streaming.
            if stream.custom_llm_provider not in ("openai", "azure"):
                raise StreamProtocolError(
                    "Unsupported LiteLLM streaming provider shape."
                )
            observed = _ProviderChunks(stream.completion_stream)
            stream.completion_stream = observed
            async for _synthetic_or_normalized in stream:
                while observed.pending:
                    yield observed.pending.popleft()
            while observed.pending:
                yield observed.pending.popleft()
        else:
            # Direct OpenAI-compatible asynchronous streams, including offline
            # SDK doubles. They contain provider chunks, never synthesized EOF.
            observed = _ProviderChunks(stream)
            stream = observed
            async for _raw in observed:
                yield observed.pending.popleft()
        # The SDK treats some post-finish read errors as EOF. Only the actual
        # upstream iterator can establish clean exhaustion for this attempt.
        if observed.failure is not None:
            raise observed.failure
        if not observed.exhausted:
            raise StreamProtocolError("SDK ended before provider stream exhaustion.")
    except BaseException as exc:
        error = exc
        if observed is not None and isinstance(observed.failure, StreamProtocolError):
            # SDK remapping cannot turn our malformed-data rejection into a
            # provider/quota failure eligible for another request.
            error = observed.failure
        primary = (
            _safe_error(error, api_key, observed is not None and observed.count > 0)
            if isinstance(error, Exception)
            else error
        )
    finally:
        if observed is not None:
            observed.pending.clear()
        if stream is not None:
            try:
                cancellation = await _cleanup(_close(stream))
                if observed is not None and observed.close_failed:
                    if cancellation is not None:
                        cancellation.add_note("Provider stream cleanup failed.")
                        raise cancellation
                    raise StreamProtocolError("Provider stream cleanup failed.")
            except BaseException as cleanup_error:
                if primary is None:
                    if isinstance(cleanup_error, asyncio.CancelledError):
                        raise
                    raise StreamProtocolError(
                        "Provider stream cleanup failed."
                    ) from None
                primary.add_note("Provider stream cleanup failed.")
            else:
                if primary is None and cancellation is not None:
                    raise cancellation
    # Raise outside the handler: raising the SDK's original error while handling
    # its wrapper would overwrite its structured quota chain (or form a cycle).
    if primary is not None:
        raise primary from None
