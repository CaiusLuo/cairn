import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cairn.core.context import ContextBudget, ContextBuilder, ContextRequest
from cairn.core.llm import LLMClient
from cairn.core.memory.context import MemoryContext
from cairn.core.model_manager import ModelConfig, ModelManager
from cairn.core.models import LLMResponse, Message
from cairn.infra.llm.errors import _short_diagnostic, is_fallback_error

if TYPE_CHECKING:
    from cairn.runtime.agent import Agent


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

    The caller keeps ``Agent.llm`` and ``Agent.context_builder`` bound to
    the first ID of ``ModelManager.current_model()`` (``cli.py`` rebinds both
    together on ``/model use``). Only that candidate reuses them; all other IDs
    build a runtime through ``runtime_factory``. Every attempt therefore keeps
    its own client, tokenizer, context budget admission and output limit.
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
        agent: "Agent",
        model: ModelConfig | None,
        model_id: str | None,
    ) -> tuple[LLMClient, ContextBuilder]:
        selected = self.manager.current_model() if self.manager is not None else None
        if model is None or (model == selected and model_id == model.model_ids[0]):
            return agent.llm, agent.context_builder
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
        agent: "Agent",
        system_messages: list[Message],
        history: list[Message],
        current_turn_start: int,
        tools: list[dict[str, Any]],
        project_memory: MemoryContext | None = None,
        on_request: Callable[
            [ContextRequest, ContextBudget, ModelConfig | None, str | None, int], None
        ]
        | None = None,
        on_fallback: Callable[[Exception], None] | None = None,
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
                llm, builder = self._runtime_for(agent, model, model_id)
                # Admission failures are not provider failures and must not retry.
                request = builder.build(
                    system_messages=system_messages,
                    history=history,
                    current_turn_start=current_turn_start,
                    tools=tools,
                    project_memory=project_memory,
                )
                attempt += 1
                if on_request is not None:
                    on_request(request, builder.budget, model, model_id, attempt)
                try:
                    response = await llm.generate(
                        messages=request.messages,
                        tools=tools,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._note_failure(model_id, exc)
                    if model_id is None or not is_fallback_error(exc):
                        raise

                    errors.append((model_id, exc))
                    if on_fallback is not None:
                        on_fallback(exc)
                    continue

                if model_id is not None:
                    self._last_failures.pop(model_id, None)
                return request, response

        raise AllModelsUnavailable(errors) from None
