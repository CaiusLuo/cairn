"""Offline subprocess fixture for Pi protocol tests and manual terminal smoke.

Run via python -m tests.support.pi_backend. No provider SDK or network is used.
"""

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cairn.core.context import ContextBudget, ContextBuilder, EstimatedTokenCounter
from cairn.core.llm import LLMClient, LLMStreamChunk, ToolCallDelta
from cairn.core.model_manager import ModelManager
from cairn.core.models import LLMResponse, Message, ToolResult
from cairn.core.provider_catalog import NamedProvider
from cairn.infra.tools.base import ToolExecutionContext
from cairn.runtime.model_executor import ModelExecutor
from cairn.runtime.provider_runtime import ProviderRuntime
from cairn.terminal.bridge import main


class OfflineClient:
    def __init__(self, model: str) -> None:
        self.model = model

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        raise AssertionError("streaming required")

    async def stream_generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> AsyncGenerator[LLMStreamChunk, None]:
        prompt = next(m.content or "" for m in reversed(messages) if m.role == "user")
        if prompt == "fail":
            raise RuntimeError("SECRET-MUST-NOT-LEAK")
        yield LLMStreamChunk(reasoning_text="Offline thinking. ")
        await asyncio.sleep(0.03)
        if messages[-1].role != "tool" and prompt in {"tool", "permission", "slow"}:
            yield LLMStreamChunk(text="Checking workspace. ")
            args: dict[str, Any] = {"command": prompt}
            if prompt == "permission":
                args.update(
                    network_access=True, justification="Offline permission test"
                )
            yield LLMStreamChunk(
                tool_calls=(
                    ToolCallDelta(
                        index=0,
                        id="offline-call",
                        name="bash",
                        arguments=json.dumps(args),
                    ),
                )
            )
            yield LLMStreamChunk(finish_reason="tool_calls")
            return
        for word in f"{self.model}: offline response history={sum(m.role == 'user' for m in messages)}".split():
            yield LLMStreamChunk(text=word + " ")
            await asyncio.sleep(0.03)
        yield LLMStreamChunk(finish_reason="stop")


def offline_runtime(
    provider: NamedProvider,
    credential: str,
    context_budget: ContextBudget,
    *,
    group: str | None = None,
) -> ProviderRuntime:
    if provider.name == "broken":
        raise RuntimeError("SECRET-MUST-NOT-LEAK")
    manager = ModelManager(provider.config)
    if group:
        manager.select_model(group)

    def factory(model: str) -> tuple[LLMClient, ContextBuilder]:
        return OfflineClient(model), ContextBuilder(
            budget=context_budget, counter=EstimatedTokenCounter()
        )

    llm, context = factory(manager.current_model().model_ids[0])
    return ProviderRuntime(
        provider, manager, ModelExecutor(manager, factory), llm, context, factory
    )


async def offline_tool(
    self: Any, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
) -> ToolResult:
    if arguments["command"] == "permission":
        assert context and context.network_access
        Path("executed").write_text("authorized")
    if arguments["command"] == "slow":
        try:
            Path("started").write_text("yes")
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.2)
            Path("cleaned").write_text("yes")
    return ToolResult(stdout="offline tool result\n" + "x" * 5000, exit_code=0)


if __name__ == "__main__":
    with (
        patch("cairn.terminal.bridge.build_provider_runtime", offline_runtime),
        patch("cairn.runtime.provider_runtime.build_provider_runtime", offline_runtime),
        patch("cairn.infra.tools.bash.BashTool.execute", offline_tool),
    ):
        main()
