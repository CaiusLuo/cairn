import asyncio
import json
import socket
from typing import Any

import pytest
from evaluation.langgraph_spike import (
    FixtureReadTool,
    LangGraphSpike,
    OfflineModel,
    compare_offline,
)

from cairn.core.budget import RunBudget, RunBudgetExceeded
from cairn.core.models import LLMResponse, Message, ToolCall, ToolResult
from cairn.core.permissions import (
    PermissionDecision,
    PermissionResult,
    PermissionSource,
)
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.tools.registry import ToolRegistry
from tests.support.runtime import SequenceLLM


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("The spike must stay offline")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def registry(tool: FixtureReadTool | None = None) -> ToolRegistry:
    result = ToolRegistry()
    if tool is not None:
        result.register_tool(tool)
    return result


def call(name: str, arguments: dict[str, Any] | None = None) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="call-1", name=name, arguments=arguments or {})]
    )


def test_chat_finishes_without_a_tool() -> None:
    model = SequenceLLM([LLMResponse(content="hello")])
    spike = LangGraphSpike(model, registry())
    result = asyncio.run(spike.run("hi", budget=RunBudget(1)))
    assert result.answer == "hello"
    assert result.model_steps == 1
    assert result.nodes == ("model",)
    assert [m.role for m in spike.messages] == ["user", "assistant"]


def test_tool_result_reaches_model_through_same_turn_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even an environment requesting tracing must not cause an export.
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    results = asyncio.run(compare_offline())
    old, graph = results["direct_agent"], results["facade"]
    assert old["answer"] == graph["answer"] == "Answer: offline fixture"
    assert old["model_requests"] == graph["model_requests"] == 2
    assert old["tool_executions"] == graph["tool_executions"] == 1
    assert graph["graph_nodes"] == ("model", "tools", "model")


class ShellDouble(FixtureReadTool):
    name = "bash"

    def validate(self, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments.get("command"), str):
            raise InvalidArguments("command must be a string")


@pytest.mark.parametrize("handler_present", [False, True])
def test_denied_or_missing_authority_never_executes(handler_present: bool) -> None:
    tool = ShellDouble()
    approvals: list[ToolCall] = []

    def deny(tool_call: ToolCall) -> PermissionResult:
        approvals.append(tool_call)
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=False,
            source=PermissionSource.USER_DENIED,
        )

    model = SequenceLLM(
        [
            call("bash", {"command": "echo fake", "confirmed": True}),
            LLMResponse(content="denied"),
        ]
    )
    spike = LangGraphSpike(
        model, registry(tool), permission_handler=deny if handler_present else None
    )
    asyncio.run(spike.run("run", budget=RunBudget(2)))
    assert tool.calls == 0
    assert len(approvals) == int(handler_present)
    failure = json.loads(model.calls[1][0][-1].content or "")
    assert failure["type"] == (
        "PermissionDenied" if handler_present else "PermissionRequired"
    )


@pytest.mark.parametrize(
    ("name", "arguments", "failure_type"),
    [
        ("missing", {}, "ToolNotFound"),
        ("bash", {"command": 42}, "InvalidArguments"),
        ("bash", {"command": "sudo true"}, "PolicyDenied"),
    ],
)
def test_preflight_and_policy_deny_precede_approval(
    name: str, arguments: dict[str, Any], failure_type: str
) -> None:
    def must_not_approve(tool_call: ToolCall) -> PermissionResult:
        raise AssertionError("approval must not be requested")

    tool = ShellDouble()
    model = SequenceLLM([call(name, arguments), LLMResponse(content="handled")])
    spike = LangGraphSpike(model, registry(tool), permission_handler=must_not_approve)
    asyncio.run(spike.run("run", budget=RunBudget(2)))
    assert tool.calls == 0
    assert json.loads(model.calls[1][0][-1].content or "")["type"] == failure_type


@pytest.mark.parametrize("nonzero", [False, True])
def test_tool_failure_is_visible_to_next_model(nonzero: bool) -> None:
    class Failure(FixtureReadTool):
        async def execute(
            self,
            arguments: dict[str, Any],
            *,
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            self.calls += 1
            if nonzero:
                return ToolResult(stderr="failed", exit_code=7)
            raise RuntimeError("fake tool failed")

    tool = Failure()
    model = SequenceLLM([call(tool.name), LLMResponse(content="failure handled")])
    spike = LangGraphSpike(model, registry(tool))
    asyncio.run(spike.run("run", budget=RunBudget(2)))
    content = json.loads(model.calls[1][0][-1].content or "")
    assert content["exit_code"] == 7 if nonzero else content["type"] == "RuntimeError"
    assert tool.calls == 1  # No generic retry after an unknown effect.


@pytest.mark.parametrize("limit", [1, 3])
def test_budget_counts_model_steps_and_settles_last_batch(limit: int) -> None:
    tool = FixtureReadTool()
    model = SequenceLLM([call(tool.name) for _ in range(limit)])
    spike = LangGraphSpike(model, registry(tool))
    with pytest.raises(RunBudgetExceeded) as caught:
        asyncio.run(spike.run("repeat", budget=RunBudget(limit)))
    assert caught.value.used == caught.value.limit == limit
    assert len(model.calls) == tool.calls == limit
    assert spike.messages[-1].role == "tool"


class Blocker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False

    async def wait(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleaning.set()
            await self.release.wait()
            self.finished = True


@pytest.mark.parametrize("phase", ["model", "tool", "approval"])
def test_repeated_cancellation_waits_for_owned_cleanup(phase: str) -> None:
    async def scenario() -> None:
        blocker = Blocker()

        class SlowModel(OfflineModel):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                await blocker.wait()
                raise AssertionError("unreachable")

        class SlowTool(ShellDouble):
            async def execute(
                self,
                arguments: dict[str, Any],
                *,
                context: ToolExecutionContext | None = None,
            ) -> ToolResult:
                self.calls += 1
                await blocker.wait()
                raise AssertionError("unreachable")

        class Approval:
            def __call__(self, tool_call: ToolCall) -> PermissionResult:
                return PermissionResult(
                    policy_decision=PermissionDecision.ASK, allowed=True
                )

            async def authorize_tool_call_async(
                self, tool_call: ToolCall
            ) -> PermissionResult:
                await blocker.wait()
                raise AssertionError("unreachable")

        response = LLMResponse(
            tool_calls=[
                ToolCall(id=f"call-{n}", name="bash", arguments={"command": "fake"})
                for n in (1, 2)
            ]
        )
        tool = SlowTool()
        spike = LangGraphSpike(
            SlowModel() if phase == "model" else SequenceLLM([response]),
            registry(tool),
            permission_handler=Approval()
            if phase == "approval"
            else Approval().__call__,
        )
        task = asyncio.create_task(spike.run("run", budget=RunBudget(3)))
        await asyncio.wait_for(blocker.started.wait(), 2)
        task.cancel("operator stop")
        await asyncio.wait_for(blocker.cleaning.wait(), 2)
        task.cancel("second stop")
        await asyncio.sleep(0)
        assert not task.done()
        blocker.release.set()
        with pytest.raises(asyncio.CancelledError, match="operator stop"):
            await asyncio.wait_for(task, 2)
        assert blocker.finished
        assert tool.calls == int(phase == "tool")
        if phase == "tool":
            facts = [
                json.loads(m.content or "") for m in spike.messages if m.role == "tool"
            ]
            assert [f["type"] for f in facts] == ["ToolCancelled", "TurnAborted"]
        else:
            assert spike.messages == ()

    asyncio.run(scenario())


def test_no_implicit_resume_and_message_snapshots_are_isolated() -> None:
    spike = LangGraphSpike(SequenceLLM([LLMResponse(content="ok")]), registry())
    asyncio.run(spike.run("hi", budget=RunBudget(1)))
    snapshot = spike.messages
    snapshot[0].content = "changed"
    assert spike.messages[0].content == "hi"
    with pytest.raises(RuntimeError, match="only once"):
        asyncio.run(spike.run("again", budget=RunBudget(1)))


@pytest.mark.parametrize("after_tool", [False, True])
def test_model_failure_retains_executed_tool_facts(after_tool: bool) -> None:
    class BrokenModel(OfflineModel):
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            if not after_tool or messages[-1].role == "tool":
                raise RuntimeError("fake model failed")
            return await super().generate(messages, tools)

    tool = FixtureReadTool()
    spike = LangGraphSpike(BrokenModel(), registry(tool))
    with pytest.raises(RuntimeError, match="fake model failed"):
        asyncio.run(spike.run("run", budget=RunBudget(3)))
    assert tool.calls == int(after_tool)
    assert [m.role for m in spike.messages] == (
        ["user", "assistant", "tool"] if after_tool else []
    )
