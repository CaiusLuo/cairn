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

#: Code for one model's free-tier allowance (Alibaba Cloud Model Studio). It is
#: model-level: another configured model keeps its own free-tier allowance.
_FREE_TIER_QUOTA_CODES = frozenset({"AllocationQuota.FreeTierOnly"})

#: Fragments of the documented Model Studio 403 message, matched only when
#: LiteLLM rebuilt the error without the provider payload. Both must be present,
#: which no generic 403, credential failure or unrelated APIError satisfies.
_FREE_TIER_403_SIGNATURE = ("free quota exhausted", "use free tier only")

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


def _structured_error_fields(error: BaseException) -> set[str]:
    """Collect structured ``code``/``type`` values along the exception chain.

    LiteLLM 1.101 rebuilds provider errors with ``body=None`` and synthetic
    ``code``/``type`` values; the provider's parsed payload survives only on the
    chained original exception. Only structured fields are read; message text is
    never parsed here.
    """
    fields: set[str] = set()
    current: BaseException | None = error
    while current is not None:
        sources: list[object] = [current]
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            sources.extend((body, body.get("error")))
        for source in sources:
            if isinstance(source, dict):
                values = (source.get("code"), source.get("type"))
            else:
                values = (getattr(source, "code", None), getattr(source, "type", None))
            fields.update(value for value in values if isinstance(value, str))
        current = current.__context__
    return fields


def _records_shared_quota(error: BaseException) -> bool:
    """Whether structured provider fields mark the account out of quota."""
    return bool(_structured_error_fields(error) & _SHARED_QUOTA_CODES)


def _is_model_free_tier_exhaustion(error: BaseException) -> bool:
    """Whether one model's free-tier allowance is exhausted.

    Prefer the structured provider code, which survives on the chained original
    exception. LiteLLM otherwise rebuilds a bare ``APIError`` with ``body=None``
    (its openai mapper has no 403 branch), so the documented 403 message is the
    remaining narrow signature.
    """
    fields = _structured_error_fields(error)
    if fields & _SHARED_QUOTA_CODES:
        return False
    if fields & _FREE_TIER_QUOTA_CODES:
        return True
    if getattr(error, "status_code", None) != 403:
        return False
    text = str(error).lower()
    return all(fragment in text for fragment in _FREE_TIER_403_SIGNATURE)


def is_fallback_error(error: BaseException) -> bool:
    """Whether trying the next candidate model could plausibly succeed.

    V1 falls back on a vendor rate limit: LiteLLM reports that the upstream
    provider throttled this model (not LiteLLM's own key/team/model limiter) and
    no structured payload marks the account itself as out of quota. It also
    falls back when one model's free-tier allowance is exhausted (Alibaba Cloud
    Model Studio ``AllocationQuota.FreeTierOnly``), which another candidate's own
    allowance can clear.

    Everything else propagates: cancellation, invalid requests, authentication
    failures, context overflow, timeouts, service-unavailable responses and
    ambiguous billing failures. Timeouts and 503s are not model-level, and every
    candidate shares this endpoint and credential, so retrying cannot be shown to
    help.

    LiteLLM's 1.101 SDK path discards the provider payload, so a vendor 429 or
    the free-tier 403 cannot always be classified from structured fields and is
    then matched against its documented message. That fallback stays bounded to
    the configured candidates and never persists, so the residual ambiguity is
    cheap.
    """
    # Keep the provider SDK out of the core/CLI import path until needed.
    from litellm.exceptions import (
        RateLimitError,
        RateLimitErrorCategory,
        RateLimitType,
    )

    if not isinstance(error, RateLimitError):
        return _is_model_free_tier_exhaustion(error)
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
