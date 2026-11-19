"""Recovery must not depend on the diagnostic timeline being writable."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, Message, ToolCall, ToolResult
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.agent.tools.arguments import function_schema
from cairn.development.catalogue import RuntimeCatalogue
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository
from tests.support.pg import database_for


class ExplodingTool:
    """A tool failure whose side effect the host cannot rule out."""

    name = "explode"
    description = "Fail after an unknown side effect."

    def schema(self) -> dict[str, Any]:
        return function_schema(
            self.name,
            self.description,
            {"type": "object", "properties": {}, "additionalProperties": False},
        )

    def validate(self, arguments: dict[str, Any]) -> None:
        if arguments:
            raise ValueError("explode takes no arguments")

    async def execute(
        self, arguments: dict[str, Any], *, context: object | None = None
    ) -> ToolResult:
        raise RuntimeError("driver acknowledged the write, outcome unknown")


class UnknownEffectModel(OfflineDevelopmentModel):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del tools
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="call-1", name="explode", arguments={})],
            )
        return LLMResponse(content="must never be requested after unknown effects")


def test_unknown_effect_still_requires_recovery_when_timeline_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RuntimeCatalogue.record

    def broken_record(
        catalogue: RuntimeCatalogue,
        task: Any,
        kind: str,
        data: dict[str, Any] | None = None,
    ) -> Any:
        if kind == "tool_error":
            raise OSError("diagnostic timeline unavailable")
        return record(catalogue, task, kind, data)

    monkeypatch.setattr(RuntimeCatalogue, "record", broken_record)

    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    app = runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="zzzz",
        database=database_for(tmp_path),
    )
    model = UnknownEffectModel()
    app.state.execution.llm_factory = lambda: model
    app.state.execution.configure_agent = lambda agent, _task: (
        agent.tools.register_tool(ExplodingTool())
    )
    api = RuntimeClient(
        "http://127.0.0.1:8777",
        "zzzz",
        transport=httpx.ASGITransport(app=app),
    )

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            entry = await api.create_development(
                "Perform a risky write", "web", "unknown-effect-1"
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)

            task = await api.inspect(entry["task_id"])
            assert task["status"] == "recovery_required", task
            assert task["diagnostic"] == "effects_require_host_review"
            assert model.calls == 1, "no second model request may replay the effect"
            page = await api.request("GET", f"tasks/{entry['task_id']}/events")
            kinds = [event["kind"] for event in page["events"]]
            assert "tool_error" not in kinds
            assert "interrupted" in kinds

    asyncio.run(scenario())
