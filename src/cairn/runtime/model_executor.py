import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from cairn.core.compaction import CompactionRecord
from cairn.core.context import ContextBudget, ContextBuilder, ContextRequest
from cairn.core.events import Event
from cairn.core.llm import LLMClient, StreamingLLMClient, StreamProtocolError
from cairn.core.memory.context import MemoryContext
from cairn.core.model_manager import ModelConfig, ModelManager
from cairn.core.models import LLMResponse, Message
from cairn.infra.llm.errors import _short_diagnostic, is_fallback_error
from cairn.runtime.lifecycle import await_owned, settle_owned
from cairn.runtime.response_assembly import ResponseAssembler


class StreamObserverError(RuntimeError):
    """Presentation failures are never eligible provider fallback errors."""


def _notify(observer: Callable[[Event], None] | None, event: Event) -> None:
    if observer is not None:
        try:
            observer(event)
        except Exception:
            raise StreamObserverError("Streaming observer failed.") from None



def _usage_fields(response: LLMResponse) -> dict[str, int]:
    """The token counts this response reported, for the status line to adopt.

    Omitted rather than zeroed when the provider reported nothing, so a reader is
    never shown a measurement that was not made.
    """
    usage = response.usage
    if usage is None:
        return {}
    fields: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens", "cached_tokens"):
        value = getattr(usage, name)
        if type(value) is int and value >= 0:
            fields[name] = value
    return fields

async def _assemble_stream(
    client: StreamingLLMClient,
    request: ContextRequest,
    tools: list[dict[str, Any]],
    observer: Callable[[Event], None] | None,
    attribution: dict[str, Any],
) -> LLMResponse:
    stream = client.stream_generate(request.messages, tools)
    assembler = ResponseAssembler()
    primary: BaseException | None = None
    try:
        _notify(observer, Event(type="model_stream_start", data=attribution))
        async for chunk in stream:
            assembler.add(chunk)
            if chunk.reasoning_text:
                _notify(
                    observer,
                    Event(
                        type="model_reasoning_delta",
                        data={**attribution, "text": chunk.reasoning_text},
                    ),
                )
            if chunk.text:
                _notify(
                    observer,
                    Event(
                        type="model_text_delta",
                        data={**attribution, "text": chunk.text},
                    ),
                )
        response = assembler.finish()
    except BaseException as exc:
        primary = exc
        raise
    finally:
        assembler.discard()
        # Even an observer failure must close the suspended producer. Cleanup is
        # owned work and cannot be detached by a second caller cancellation.
        closing = asyncio.create_task(stream.aclose())
        cancellation = await await_owned(closing)
        try:
            closing.result()
        except BaseException:
            if primary is None:
                raise StreamProtocolError("Stream cleanup failed.") from None
            primary.add_note("Stream cleanup failed.")
        if primary is None and cancellation is not None:
            raise cancellation
    return response


class AllModelsUnavailable(RuntimeError):
    """Retain ordered attempt failures without exposing provider error text."""

    def __init__(self, errors: Sequence[tuple[str, Exception]]) -> None:
        self.errors = tuple(errors)
        if self.errors:
            attempts = ", ".join(
                f"{name!r} ({type(error).__name__})" for name, error in self.errors
            )
            message = f"All candidate models are unavailable: {attempts}."
        else:
            message = "No candidate models are available."
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ModelFailure:
    """Most recent provider failure recorded for one model in this session.

    Display-only metadata for ``/model list``. It never removes, reorders or
    skips a candidate, is not persisted, and is cleared when that model answers.
    """

    category: str
    detail: str


class ModelExecutor:
    """Try each group's IDs in order, then the next group in ``candidates()``.

    The caller owns one already-bound "primary" runtime — the LLM client and
    context builder currently selected for the session — and passes it in. Only
    the first ID of ``ModelManager.current_model()`` reuses it; all other IDs
    build their own through ``runtime_factory``. Every attempt therefore keeps
    its own client, tokenizer, context budget admission and output limit.

    The executor depends on those two collaborators rather than on the Agent
    aggregate, so it can run for any caller that can supply a runtime.
    """

    def __init__(
        self,
        manager: ModelManager | None = None,
        runtime_factory: Callable[[str], tuple[LLMClient, ContextBuilder]]
        | None = None,
    ) -> None:
        if (manager is None) != (runtime_factory is None):
            raise ValueError(
                "ModelManager and runtime_factory must be supplied together."
            )
        self.manager = manager
        self.runtime_factory = runtime_factory
        self._last_failures: dict[str, ModelFailure] = {}

    def failure_for(self, model_id: str) -> ModelFailure | None:
        """Most recent provider failure recorded for this concrete ID, if any."""
        return self._last_failures.get(model_id)

    def _runtime_for(
        self,
        primary_runtime: tuple[LLMClient, ContextBuilder],
        model: ModelConfig | None,
        model_id: str | None,
    ) -> tuple[LLMClient, ContextBuilder]:
        selected = self.manager.current_model() if self.manager is not None else None
        if model is None or (model == selected and model_id == model.model_ids[0]):
            return primary_runtime
        assert self.runtime_factory is not None
        assert model_id is not None
        return self.runtime_factory(model_id)

    def _note_failure(self, model_id: str | None, error: Exception) -> None:
        if model_id is None:
            return
        self._last_failures[model_id] = ModelFailure(
            category=type(error).__name__,
            detail=_short_diagnostic(error),
        )

    async def execute(
        self,
        *,
        primary_runtime: tuple[LLMClient, ContextBuilder],
        system_messages: list[Message],
        history: list[Message],
        current_turn_start: int,
        tools: list[dict[str, Any]],
        project_memory: MemoryContext | None = None,
        compaction: "CompactionRecord | None" = None,
        on_request: Callable[
            [ContextRequest, ContextBudget, ModelConfig | None, str | None, int], None
        ]
        | None = None,
        on_fallback: Callable[[Exception], None] | None = None,
        on_stream: Callable[[Event], None] | None = None,
    ) -> tuple[ContextRequest, LLMResponse]:
        errors: list[tuple[str, Exception]] = []

        candidates: Sequence[ModelConfig | None] = (
            self.manager.candidates() if self.manager is not None else (None,)
        )
        attempt = 0
        for model in candidates:
            model_ids: Sequence[str | None] = (
                model.model_ids if model is not None else (None,)
            )
            for model_id in model_ids:
                llm, builder = self._runtime_for(primary_runtime, model, model_id)
                # Admission failures are not provider failures and must not retry.
                # Every candidate is admitted against the same substitution, so a
                # fallback model reads the conversation the primary one would have.
                request = builder.build(
                    system_messages=system_messages,
                    history=history,
                    current_turn_start=current_turn_start,
                    tools=tools,
                    project_memory=project_memory,
                    compaction=compaction,
                )
                attempt += 1
                if on_request is not None:
                    on_request(request, builder.budget, model, model_id, attempt)
                streaming = isinstance(llm, StreamingLLMClient) and (
                    getattr(llm, "supports_streaming", True) is True
                )
                attribution = {"attempt": attempt, "model_id": model_id}
                try:
                    if streaming and isinstance(llm, StreamingLLMClient):
                        collecting = asyncio.create_task(
                            _assemble_stream(
                                llm, request, tools, on_stream, attribution
                            )
                        )
                        try:
                            response = await asyncio.shield(collecting)
                        except asyncio.CancelledError:
                            await settle_owned(collecting)
                            raise
                    else:
                        response = await llm.generate(
                            messages=request.messages,
                            tools=tools,
                        )
                except asyncio.CancelledError:
                    if streaming:
                        # Presentation failure cannot replace a primary stop.
                        with suppress(StreamObserverError):
                            _notify(
                                on_stream,
                                Event(
                                    type="model_stream_end",
                                    data={**attribution, "status": "incomplete"},
                                ),
                            )
                    raise
                except Exception as exc:
                    self._note_failure(model_id, exc)
                    fallback = (
                        model_id is not None
                        and not isinstance(
                            exc, (StreamProtocolError, StreamObserverError)
                        )
                        and is_fallback_error(exc)
                    )
                    if streaming:
                        try:
                            _notify(
                                on_stream,
                                Event(
                                    type="model_stream_end",
                                    data={
                                        **attribution,
                                        "status": "abandoned"
                                        if fallback
                                        else "incomplete",
                                    },
                                ),
                            )
                        except StreamObserverError:
                            exc.add_note(
                                "Stream failure display could not be delivered."
                            )
                            fallback = False
                    if not fallback:
                        raise

                    assert model_id is not None
                    errors.append((model_id, exc))
                    if on_fallback is not None:
                        on_fallback(exc)
                    continue

                if streaming:
                    # The step's own measurement rides on the existing end event.
                    # A status line cannot wait for the turn total: that is only
                    # published at trace_finish, and a turn that fails publishes
                    # none, so this is the last point where a step's figures can
                    # reach a reader.
                    _notify(
                        on_stream,
                        Event(
                            type="model_stream_end",
                            data={
                                **attribution,
                                "status": "completed",
                                "context_used": request.tokens_before.tokens,
                                **_usage_fields(response),
                            },
                        ),
                    )
                if model_id is not None:
                    self._last_failures.pop(model_id, None)
                return request, response

        raise AllModelsUnavailable(errors) from None
