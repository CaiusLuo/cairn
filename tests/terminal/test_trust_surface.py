"""Asking for provider trust on the bottom surface, and what counts as consent.

Switching providers sends the whole conversation somewhere else, so the decision is
put in front of the operator with every fact that makes it meaningful: the endpoint
in full, the credential *variable* (never its value), and the models it covers.

The rule these tests protect is that nothing but an explicit Trust grants. Cancel,
Escape, end of input, a closed surface and a mismatched row all refuse, because a
mistaken Enter on a menu must not send a conversation to a host nobody approved.
"""

import asyncio
from typing import Any, cast

import pytest

from cairn.core.model_manager import ModelConfig, ProviderConfig
from cairn.core.provider_catalog import NamedProvider
from cairn.terminal.action import Action
from cairn.terminal.cli import (
    TRUST_ROW,
    _ask_in_surface,
    _permission_prompt_for,
    _trust_decision,
)


def _provider() -> NamedProvider:
    return NamedProvider(
        name="bailian",
        config=ProviderConfig(
            base_url="https://llm.example.test/compatible-mode/v1",
            api_key_env="BAILIAN_API_KEY",
            model_config=(
                ModelConfig("flash", ("openai/qwen-flash", "openai/qwen-backup")),
            ),
        ),
    )


class _Frame:
    """A frame that records what the surface was asked to show."""

    def __init__(self) -> None:
        self.shown: list[Action | None] = []
        self.on_action_choose: Any = None
        self.on_action_command: Any = None
        self.on_action_move: Any = None

    def show_action(self, action: Action | None) -> None:
        self.shown.append(action)


def _answer_with(frame: _Frame, row: str | None) -> None:
    """Press Enter on ``row``, or close the surface when it is ``None``."""
    assert frame.on_action_choose is not None
    if row is None:
        # A closed surface reports an empty command: no answer at all.
        assert frame.on_action_command is not None
        frame.on_action_command("")
        return
    frame.on_action_choose(row)


def test_the_decision_states_every_fact_in_full() -> None:
    provider = _provider()

    action = _trust_decision(provider, provider.config.model_config[0])

    assert provider.config.base_url in action.message
    assert provider.config.api_key_env in action.message
    assert "openai/qwen-flash" in action.message
    # The credential value never appears: only the variable that holds it.
    assert "secret" not in action.message.lower()


def test_the_cursor_starts_on_declining() -> None:
    """Enter on an untouched menu must not grant anything."""
    provider = _provider()

    action = _trust_decision(provider, provider.config.model_config[0])

    row = action.selected_row()
    assert row is not None
    assert row.cancel
    assert row.name != TRUST_ROW


def test_the_decision_is_an_authority_question() -> None:
    provider = _provider()

    action = _trust_decision(provider, provider.config.model_config[0])

    assert action.decides_authority


def test_trusting_resolves_to_consent() -> None:
    frame = _Frame()
    provider = _provider()

    async def scenario() -> bool:
        task = asyncio.create_task(
            _ask_in_surface(
                provider,
                provider.config.model_config[0],
                frame=cast(Any, frame),
            )
        )
        # The question is on screen before anyone answers.
        while not frame.shown:
            await asyncio.sleep(0)
        assert frame.shown[0] is not None
        _answer_with(frame, TRUST_ROW)
        return await task

    assert asyncio.run(scenario()) is True
    # The surface is cleared afterwards: the question is not left standing.
    assert frame.shown[-1] is None


def test_cancelling_resolves_to_refusal() -> None:
    frame = _Frame()
    provider = _provider()

    async def scenario() -> bool:
        task = asyncio.create_task(
            _ask_in_surface(
                provider,
                provider.config.model_config[0],
                frame=cast(Any, frame),
            )
        )
        while not frame.shown:
            await asyncio.sleep(0)
        _answer_with(frame, "Cancel")
        return await task

    assert asyncio.run(scenario()) is False


def test_closing_the_surface_resolves_to_refusal() -> None:
    """Escape closes the surface; a question with no answer is not consent."""
    frame = _Frame()
    provider = _provider()

    async def scenario() -> bool:
        task = asyncio.create_task(
            _ask_in_surface(
                provider,
                provider.config.model_config[0],
                frame=cast(Any, frame),
            )
        )
        while not frame.shown:
            await asyncio.sleep(0)
        _answer_with(frame, None)
        return await task

    assert asyncio.run(scenario()) is False


def test_an_unknown_row_is_a_refusal() -> None:
    """Only the exact grant row grants; a renamed or mismatched row does not."""
    frame = _Frame()
    provider = _provider()

    async def scenario() -> bool:
        task = asyncio.create_task(
            _ask_in_surface(
                provider,
                provider.config.model_config[0],
                frame=cast(Any, frame),
            )
        )
        while not frame.shown:
            await asyncio.sleep(0)
        _answer_with(frame, "something else")
        return await task

    assert asyncio.run(scenario()) is False


def test_the_previous_key_bindings_are_restored() -> None:
    """The question owns the surface, then hands it back."""
    frame = _Frame()
    before_choose = object()
    before_command = object()
    frame.on_action_choose = before_choose
    frame.on_action_command = before_command
    provider = _provider()

    async def scenario() -> bool:
        task = asyncio.create_task(
            _ask_in_surface(
                provider,
                provider.config.model_config[0],
                frame=cast(Any, frame),
            )
        )
        while not frame.shown:
            await asyncio.sleep(0)
        _answer_with(frame, "Cancel")
        return await task

    asyncio.run(scenario())

    assert frame.on_action_choose is before_choose
    assert frame.on_action_command is before_command


def test_cancelling_provider_approval_propagates_and_restores_the_surface() -> None:
    """Task cancellation stops the caller before it can commit a provider switch."""
    frame = _Frame()
    provider = _provider()
    switched = False

    async def scenario() -> None:
        nonlocal switched

        async def approve_then_switch() -> None:
            nonlocal switched
            if await _ask_in_surface(
                provider,
                provider.config.model_config[0],
                frame=cast(Any, frame),
            ):
                switched = True

        task = asyncio.create_task(approve_then_switch())
        while not frame.shown:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
    assert switched is False
    assert frame.shown[-1] is None
    assert frame.on_action_choose is None
    assert frame.on_action_command is None
    assert frame.on_action_move is None


def test_real_runtime_permission_bridge_awaits_the_live_surface() -> None:
    """The turn permission boundary yields so the surface can receive a choice."""
    from types import SimpleNamespace

    from cairn.core.models import ToolCall
    from cairn.core.permissions import SessionPermissionHandler
    from cairn.runtime.agent import Agent
    from cairn.runtime.tool_execution import _check_tool_permission_async

    frame = _Frame()
    handler = SessionPermissionHandler()
    handler.prompt_async = _permission_prompt_for(frame)  # type: ignore[arg-type]
    call = ToolCall(id="bash-1", name="bash", arguments={"command": "pwd"})
    agent = cast(Agent, SimpleNamespace(permission_handler=handler, tracer=None))

    async def scenario() -> Any:
        check = asyncio.create_task(_check_tool_permission_async(agent, call, None))
        for _ in range(100):
            if frame.shown:
                break
            await asyncio.sleep(0)
        assert frame.shown, "permission surface was not rendered"
        action = frame.shown[0]
        assert action is not None
        selected = action.selected_row()
        assert selected is not None
        assert selected.name == "Deny"
        assert check.done() is False
        assert frame.on_action_move is not None
        frame.on_action_move(action.selected, -1)
        moved = frame.shown[-1]
        assert moved is not None
        moved_row = moved.selected_row()
        assert moved_row is not None
        assert moved_row.name == "Allow for this session"
        frame.on_action_choose("Allow for this session")
        return await asyncio.wait_for(check, timeout=1)

    result = asyncio.run(scenario())
    assert result.allowed
    assert handler.grants
    assert frame.on_action_choose is None
    assert frame.on_action_move is None
    assert frame.shown[-1] is None


@pytest.mark.parametrize("outcome", ["deny", "close", "cancel"])
def test_real_runtime_permission_bridge_fails_closed(
    outcome: str,
) -> None:
    """Deny, Esc/EOF closure, and task cancellation never grant authority."""
    from types import SimpleNamespace

    from cairn.core.models import ToolCall
    from cairn.core.permissions import SessionPermissionHandler
    from cairn.runtime.agent import Agent
    from cairn.runtime.tool_execution import _check_tool_permission_async

    frame = _Frame()
    handler = SessionPermissionHandler()
    handler.prompt_async = _permission_prompt_for(frame)  # type: ignore[arg-type]
    call = ToolCall(id="bash-1", name="bash", arguments={"command": "pwd"})
    agent = cast(Agent, SimpleNamespace(permission_handler=handler, tracer=None))

    async def scenario() -> Any:
        check = asyncio.create_task(_check_tool_permission_async(agent, call, None))
        for _ in range(100):
            if frame.shown:
                break
            await asyncio.sleep(0)
        assert frame.shown, "permission surface was not rendered"
        if outcome == "close":
            assert frame.on_action_command is not None
            frame.on_action_command("")
        elif outcome == "deny":
            assert frame.on_action_choose is not None
            frame.on_action_choose("Deny")
        else:
            check.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(check, timeout=1)
            return None
        return await asyncio.wait_for(check, timeout=1)

    result = asyncio.run(scenario())
    if outcome != "cancel":
        assert result.allowed is False
    assert not handler.grants
    assert frame.on_action_choose is None
    assert frame.on_action_move is None
    assert frame.on_action_command is None
    assert frame.shown[-1] is None


def test_cancelling_a_real_turn_during_approval_does_not_advance_or_execute() -> None:
    """A stopped ASK rolls back without another model call or an unbalanced span."""
    from cairn.core.models import LLMResponse, ToolCall
    from cairn.core.permissions import SessionPermissionHandler
    from cairn.infra.observability.tracer import Tracer
    from cairn.runtime.loop import run_turn
    from tests.support.runtime import (
        TEST_BUDGET,
        RecordingSink,
        RecordingTool,
        SequenceLLM,
        make_agent,
    )

    class BashTool(RecordingTool):
        name = "bash"

    frame = _Frame()
    handler = SessionPermissionHandler()
    handler.prompt_async = _permission_prompt_for(frame)  # type: ignore[arg-type]
    tool = BashTool()
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="pending-bash",
                        name="bash",
                        arguments={"command": "pwd"},
                    )
                ]
            ),
            LLMResponse(content="must not be requested"),
        ]
    )
    agent = make_agent(llm, tool)
    agent.permission_handler = handler
    sink = RecordingSink()
    agent.tracer = Tracer(sink)

    async def scenario() -> None:
        turn = asyncio.create_task(run_turn(agent, "run command", budget=TEST_BUDGET))
        for _ in range(100):
            if frame.shown:
                break
            await asyncio.sleep(0)
        assert frame.shown, "permission surface was not rendered"
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, timeout=1)

    asyncio.run(scenario())

    assert len(llm.calls) == 1
    assert tool.calls == []
    assert not handler.grants
    assert agent.state.messages == []
    assert frame.on_action_choose is None
    assert frame.on_action_command is None
    assert frame.on_action_move is None
    assert frame.shown[-1] is None
    assert all(span.end_time is not None for span in sink.spans)
    permission_spans = [span for span in sink.spans if span.name == "permission.check"]
    assert len(permission_spans) == 1
    assert permission_spans[0].status.value == "error"
    assert "CancelledError" in (permission_spans[0].error or "")
    assert not [span for span in sink.spans if span.name == "tool.execute"]


# -- tool capability approval -------------------------------------------
#
# A tool approval is the same kind of question: it grants authority, so it is asked
# on the same surface, states the concrete command or paths, and starts on Deny.


def _request(*, command: str = "ls", paths: tuple[str, ...] = ()) -> Any:
    from cairn.core.models import ToolCall
    from cairn.core.permissions import PermissionCapability, PermissionRequest

    return PermissionRequest(
        capability=PermissionCapability.LOCAL_SHELL,
        justification="run a shell command",
        tool_call=ToolCall(id="c1", name="bash", arguments={"command": command}),
        paths=paths,
    )


def test_the_permission_question_states_what_it_would_allow() -> None:
    from cairn.terminal.cli import _permission_decision

    action = _permission_decision(_request(command="git status", paths=("/tmp/x",)))

    assert "local_shell" in action.message
    assert "git status" in action.message
    assert "/tmp/x" in action.message


def test_the_permission_cursor_starts_on_deny() -> None:
    """Enter on an untouched approval must not grant a capability."""
    from cairn.terminal.cli import _permission_decision

    action = _permission_decision(_request())

    row = action.selected_row()
    assert row is not None
    assert row.cancel
    assert action.decides_authority


def test_denying_is_a_denial() -> None:
    from cairn.core.permissions import PermissionChoice
    from cairn.terminal.cli import _ask_permission_in_surface, _permission_decision

    frame = _Frame()

    async def scenario() -> PermissionChoice:
        task = asyncio.create_task(
            _ask_permission_in_surface(_request(), frame=cast(Any, frame))
        )
        while not frame.shown:
            await asyncio.sleep(0)
        deny = _permission_decision(_request()).selected_row()
        assert deny is not None
        _answer_with(frame, deny.name)
        return await task

    assert asyncio.run(scenario()) is PermissionChoice.DENY


def test_allowing_once_is_a_single_grant() -> None:
    from cairn.core.permissions import PermissionChoice
    from cairn.terminal.cli import PERMISSION_ALLOW_ONCE, _ask_permission_in_surface

    frame = _Frame()

    async def scenario() -> PermissionChoice:
        task = asyncio.create_task(
            _ask_permission_in_surface(_request(), frame=cast(Any, frame))
        )
        while not frame.shown:
            await asyncio.sleep(0)
        _answer_with(frame, PERMISSION_ALLOW_ONCE)
        return await task

    assert asyncio.run(scenario()) is PermissionChoice.ALLOW_ONCE


def test_an_unknown_permission_row_denies() -> None:
    """Only a named allow row grants; anything else is read as a denial."""
    from cairn.core.permissions import PermissionChoice
    from cairn.terminal.cli import _ask_permission_in_surface

    frame = _Frame()

    async def scenario() -> PermissionChoice:
        task = asyncio.create_task(
            _ask_permission_in_surface(_request(), frame=cast(Any, frame))
        )
        while not frame.shown:
            await asyncio.sleep(0)
        _answer_with(frame, "something else")
        return await task

    assert asyncio.run(scenario()) is PermissionChoice.DENY


def test_closing_the_permission_question_denies() -> None:
    from cairn.core.permissions import PermissionChoice
    from cairn.terminal.cli import _ask_permission_in_surface

    frame = _Frame()

    async def scenario() -> PermissionChoice:
        task = asyncio.create_task(
            _ask_permission_in_surface(_request(), frame=cast(Any, frame))
        )
        while not frame.shown:
            await asyncio.sleep(0)
        _answer_with(frame, None)
        return await task

    assert asyncio.run(scenario()) is PermissionChoice.DENY
