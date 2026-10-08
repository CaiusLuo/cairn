import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cairn.core.context import ContextBudget, ContextBuilder, ContextRequest
from cairn.core.models import LLMResponse, Message
from cairn.llm.base import LLMClient
from cairn.llm.model_manager import ModelConfig, ModelManager

if TYPE_CHECKING:
    from cairn.core.agent import Agent

#: Provider error codes that mark the credential/account rather than one model.
#: Another candidate behind the same API key cannot clear them.
_SHARED_QUOTA_CODES = frozenset({"insufficient_quota", "billing_hard_limit_reached"})

#: ``/model list`` diagnostics stay on one bounded line.
_DIAGNOSTIC_LIMIT = 80


@dataclass(frozen=True, slots=True)
class ModelFailure:
    """Most recent provider failure recorded for one model in this session.

    Display-only metadata for ``/model list``. It never removes, reorders or
    skips a candidate, is not persisted, and is cleared when that model answers.
    """

    category: str
    detail: str


def _short_diagnostic(error: Exception) -> str:
    """Collapse a provider diagnostic to one bounded, single-line string.

    LiteLLM errors prefix their own class name in ``str()``; the category is
    already printed separately. ``LiteLLMClient`` replaces credential-bearing
    provider errors before they reach this layer, so this only normalizes
    whitespace and length.
    """
    text = " ".join(str(error).split())
    prefix = f"litellm.{type(error).__name__}: "
    if text.startswith(prefix):
        text = text[len(prefix) :]
    if len(text) <= _DIAGNOSTIC_LIMIT:
        return text
    return text[: _DIAGNOSTIC_LIMIT - 1].rstrip() + "…"


def _records_shared_quota(error: BaseException) -> bool:
    """Whether structured provider fields mark the account out of quota.

    LiteLLM 1.101 rebuilds ``RateLimitError`` with ``body=None`` and a synthetic
    ``code="429"``; the provider's parsed payload survives only on the chained
    original exception. Only structured ``code``/``type`` fields are read along
    ``__context__``; message text is never parsed.
    """
    current: BaseException | None = error
    while current is not None:
        sources: list[object] = [current]
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            sources.extend((body, body.get("error")))
        for source in sources:
            if isinstance(source, dict):
                fields = (source.get("code"), source.get("type"))
            else:
                fields = (getattr(source, "code", None), getattr(source, "type", None))
            shared = any(
                isinstance(field, str) and field in _SHARED_QUOTA_CODES
                for field in fields
            )
            if shared:
                return True
        current = current.__context__
    return False


def is_fallback_error(error: BaseException) -> bool:
    """Whether trying the next candidate model could plausibly succeed.

    V1 falls back only on a vendor rate limit: LiteLLM reports that the upstream
    provider throttled this model (not LiteLLM's own key/team/model limiter) and
    no structured payload marks the account itself as out of quota. Everything
    else propagates: cancellation, invalid requests, authentication failures,
    context overflow, timeouts, service-unavailable responses and ambiguous
    billing failures. Timeouts and 503s are not model-level, and every candidate
    shares this endpoint and credential, so retrying cannot be shown to help.

    LiteLLM's 1.101 SDK path discards the provider payload, so a vendor 429
    cannot always be classified further and is then treated as this model's
    limit. That fallback stays bounded to the configured candidates and never
    persists, so the residual ambiguity is cheap.
    """
    # Keep the provider SDK out of the core/CLI import path until needed.
    from litellm.exceptions import (
        RateLimitError,
        RateLimitErrorCategory,
        RateLimitType,
    )

    if not isinstance(error, RateLimitError):
        return False
    if error.category != RateLimitErrorCategory.VENDOR_RATE_LIMIT.value:
        # LiteLLM's own key/team/model limiter or a proxy budget stop: shared.
        return False
    if error.rate_limit_type in (
        RateLimitType.BUDGET.value,
        RateLimitType.MAX_ITERATIONS.value,
    ):
        return False
    return not _records_shared_quota(error)


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


class ModelExecutor:
    """Run one request per candidate, in ``ModelManager.candidates()`` order.

    The caller keeps ``Agent.llm`` and ``Agent.context_builder`` bound to
    ``ModelManager.current_model()`` (``cli.py`` rebinds both together on
    ``/model use``). Those two objects are the selected model's runtime, so the
    selected candidate reuses them and only fallback candidates build a runtime
    through ``runtime_factory``. Every attempt therefore keeps its own client,
    tokenizer, context budget admission and output limit.
    """

    def __init__(
        self,
        manager: ModelManager | None = None,
        runtime_factory: Callable[[ModelConfig], tuple[LLMClient, ContextBuilder]]
        | None = None,
    ) -> None:
        if (manager is None) != (runtime_factory is None):
            raise ValueError(
                "ModelManager and runtime_factory must be supplied together."
            )
        self.manager = manager
        self.runtime_factory = runtime_factory
        self._last_failures: dict[str, ModelFailure] = {}

    def failure_for(self, model_name: str) -> ModelFailure | None:
        """Most recent provider failure recorded for ``model_name``, if any."""
        return self._last_failures.get(model_name)

    def _runtime_for(
        self,
        agent: "Agent",
        model: ModelConfig | None,
    ) -> tuple[LLMClient, ContextBuilder]:
        selected = self.manager.current_model() if self.manager is not None else None
        if model is None or model == selected:
            return agent.llm, agent.context_builder
        assert self.runtime_factory is not None
        return self.runtime_factory(model)

    def _note_failure(self, model: ModelConfig | None, error: Exception) -> None:
        if model is None:
            return
        self._last_failures[model.name] = ModelFailure(
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
        on_request: Callable[
            [ContextRequest, ContextBudget, ModelConfig | None, int], None
        ]
        | None = None,
        on_fallback: Callable[[Exception], None] | None = None,
    ) -> tuple[ContextRequest, LLMResponse]:
        errors: list[tuple[str, Exception]] = []

        candidates: Sequence[ModelConfig | None] = (
            self.manager.candidates() if self.manager is not None else (None,)
        )
        for attempt, model in enumerate(candidates, start=1):
            llm, builder = self._runtime_for(agent, model)
            # Admission failures are not provider failures and must not retry.
            request = builder.build(
                system_messages=system_messages,
                history=history,
                current_turn_start=current_turn_start,
                tools=tools,
            )
            if on_request is not None:
                on_request(request, builder.budget, model, attempt)
            try:
                response = await llm.generate(
                    messages=request.messages,
                    tools=tools,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note_failure(model, exc)
                if model is None or not is_fallback_error(exc):
                    raise

                errors.append((model.name, exc))
                if on_fallback is not None:
                    on_fallback(exc)
                continue

            if model is not None:
                self._last_failures.pop(model.name, None)
            return request, response

        raise AllModelsUnavailable(errors) from None
