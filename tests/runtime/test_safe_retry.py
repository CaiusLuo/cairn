import asyncio
import errno
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from cairn.core.events import Event
from cairn.core.models import LLMResponse, ToolCall, ToolResult
from cairn.core.permissions import PermissionChoice, SessionPermissionHandler
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.base import (
    ToolExecutionContext,
    ToolExecutionFailure,
    ToolFailureKind,
    ToolFailureOutcome,
)
from cairn.infra.tools.files import ReadFileTool
from cairn.infra.tools.registry import ToolRegistry
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from tests.support.runtime import TEST_BUDGET, RecordingSink, SequenceLLM


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def _read_response(path: str) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="read-1", name="read_file", arguments={"path": path})]
    )


def _flaky_open(
    monkeypatch: pytest.MonkeyPatch, path: Path, *, failures: int
) -> list[int]:
    original = cast(Callable[..., object], Path.open)
    attempts: list[int] = []

    def open_path(candidate: Path, *args: object, **kwargs: object) -> object:
        if candidate == path:
            attempts.append(len(attempts) + 1)
            if len(attempts) <= failures:
                raise OSError(errno.EAGAIN, "injected transient read failure")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    return attempts


def test_read_retry_is_one_authorized_tool_call_and_one_model_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "answer.txt"
    path.write_text("42\n", encoding="utf-8")
    attempts = _flaky_open(monkeypatch, path, failures=2)
    clock = FakeClock()
    tool = ReadFileTool(
        Workspace(tmp_path), monotonic=clock.monotonic, sleep=clock.sleep
    )
    registry = ToolRegistry()
    registry.register_tool(tool)
    events: list[Event] = []
    sink = RecordingSink()

    def handle_event(event: Event) -> None:
        events.append(event)

    agent = Agent(
        llm=SequenceLLM([_read_response(path.name), LLMResponse(content="done")]),
        tools=registry,
        event_handler=handle_event,
        tracer=Tracer(sink),
    )

    assert asyncio.run(run_turn(agent, "read it", budget=TEST_BUDGET)) == "done"

    assert attempts == [1, 2, 3]
    assert clock.sleeps == [0.01, 0.02]
    assert [event.type for event in events].count("tool_call") == 1
    assert [event.type for event in events].count("tool_result") == 1
    assert not [event for event in events if event.type == "tool_error"]
    assert len([span for span in sink.spans if span.name == "permission.check"]) == 1
    assert len([span for span in sink.spans if span.name == "tool.execute"]) == 1
    assert json.loads(agent.state.messages[2].content or "")["stdout"] == "42\n"


class EffectfulFailureTool:
    name = "bash"
    description = "Write once, then report an injected transient failure."

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def schema(self) -> dict[str, Any]:
        return {"name": self.name}

    def validate(self, arguments: dict[str, Any]) -> None:
        pass

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        self.calls += 1
        self.path.write_text(f"attempt {self.calls}\n", encoding="utf-8")
        raise ToolExecutionFailure(
            ToolFailureOutcome(
                kind=ToolFailureKind.TRANSIENT_BEFORE_EFFECT,
                error_type="InjectedTransient",
                message="injected failure after an effect",
            )
        )


def test_generic_tool_execution_never_retries_an_effectful_or_unknown_tool(
    tmp_path: Path,
) -> None:
    effect = tmp_path / "effect.txt"
    tool = EffectfulFailureTool(effect)
    registry = ToolRegistry()
    registry.register_tool(tool)
    events: list[Event] = []
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="write-1",
                        name="bash",
                        arguments={"command": "write once"},
                    )
                ]
            ),
            LLMResponse(content="stopped"),
        ]
    )

    def handle_event(event: Event) -> None:
        events.append(event)

    agent = Agent(
        llm=llm,
        tools=registry,
        event_handler=handle_event,
        # A local shell is approved once per session; without an approval the
        # command would never run and the retry contract could not be observed.
        permission_handler=SessionPermissionHandler(
            lambda request: PermissionChoice.ALLOW_SESSION
        ),
    )

    assert asyncio.run(run_turn(agent, "write", budget=TEST_BUDGET)) == "stopped"

    assert tool.calls == 1
    assert effect.read_text(encoding="utf-8") == "attempt 1\n"
    error = next(event for event in events if event.type == "tool_error")
    assert error.data["failure_kind"] == "transient_before_effect"
    failure = json.loads(agent.state.messages[2].content or "")
    assert failure["type"] == "InjectedTransient"
    assert failure["error"] == "injected failure after an effect"
