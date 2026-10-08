import asyncio
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from cairn.core.context import ContextBudget, ContextBuilder, ContextRequest
from cairn.core.models import LLMResponse, Message
from cairn.llm.base import LLMClient
from cairn.llm.model_manager import ModelConfig, ModelManager

if TYPE_CHECKING:
    from cairn.core.agent import Agent


def is_fallback_error(error: BaseException) -> bool:
    """Recognize provider throttling, request timeouts, and unavailability.

    Do not infer retryability from error text or arbitrary status attributes.
    Known shared billing/budget failures cannot be fixed by changing models.
    """
    # Keep the provider SDK out of the core/CLI import path until needed.
    from litellm.exceptions import RateLimitError, ServiceUnavailableError, Timeout

    if isinstance(error, RateLimitError):
        if error.category != "vendor_rate_limit" or error.rate_limit_type == "budget":
            return False
        body = error.body
        details = body.get("error", body) if isinstance(body, dict) else {}
        shared_quota_codes = ("insufficient_quota", "billing_hard_limit_reached")
        codes = [error.code, error.type]
        if isinstance(details, dict):
            codes.extend([details.get("code"), details.get("type")])
        return not any(code in shared_quota_codes for code in codes)
    return isinstance(error, (Timeout, TimeoutError, ServiceUnavailableError))


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


class ModelExecuter:
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
            if model is None:
                # Headless callers retain their injected, replaceable client/counter.
                llm, builder = agent.llm, agent.context_builder
            else:
                assert self.runtime_factory is not None
                llm, builder = self.runtime_factory(model)
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
                if model is None or not is_fallback_error(exc):
                    raise

                errors.append((model.name, exc))
                if on_fallback is not None:
                    on_fallback(exc)
                continue

            return request, response

        raise AllModelsUnavailable(errors) from None
