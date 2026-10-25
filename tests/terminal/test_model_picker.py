import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import cairn.infra.llm.token_counter as counter_module
import cairn.terminal.cli as cli_module
from cairn.core.model_manager import ModelConfig, ModelManager, ProviderConfig
from cairn.core.permissions import PermissionCapability
from cairn.infra.observability.storage import TraceStore
from cairn.terminal.action import Action
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.router import CommandRouter
from cairn.terminal.input import CliInput
from tests.support.cli_harness import BAILIAN_CREDENTIAL, configure_cli

GROUPS = (
    ModelConfig("flash", ("openai/flash", "openai/backup", "openai/last")),
    ModelConfig("plus", ("openai/plus",)),
    ModelConfig("reason", ("openai/reason",)),
)


class RecordingOutput(DummyOutput):
    def __init__(self) -> None:
        self.text = ""

    def write(self, data: str) -> None:
        self.text += data

    def write_raw(self, data: str) -> None:
        self.text += data

    def enter_alternate_screen(self) -> None:
        pytest.fail("Model selection must remain inline")


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("\x1b[A\r", "flash"),
        ("\x1b[B\r", "reason"),
        ("\r", "plus"),
        ("\x1b", None),
        ("\x03", None),
    ],
)
def test_picker_keys_and_subsequent_prompt(keys: str, expected: str | None) -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            output = RecordingOutput()
            reader = CliInput(input=pipe, output=output)
            pipe.send_text(keys)
            selected = await asyncio.wait_for(
                reader.choose_model("local", GROUPS, "plus"), 3
            )
            assert selected == expected
            assert "Provider: local" in output.text
            assert "plus (current)" in output.text
            assert "Primary: openai/flash" in output.text
            assert "Fallbacks: openai/backup -> openai/last" in output.text
            assert "Fallbacks: none" in output.text
            assert "Esc/Ctrl+C cancel" in output.text
            # A fresh ordinary prompt uses the same pipe after every exit path.
            pipe.send_text("next question\r")
            assert await asyncio.wait_for(reader.read(), 1) == "next question"
            assert "cairn>" in output.text

    asyncio.run(scenario())


def test_external_cancellation_propagates_from_picker() -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            reader = CliInput(input=pipe, output=DummyOutput())
            task = asyncio.create_task(reader.choose_model("local", GROUPS, "flash"))
            await asyncio.sleep(0.05)
            task.cancel("external cancellation")
            with pytest.raises(asyncio.CancelledError, match="external cancellation"):
                await task
            pipe.send_text("fresh\r")
            assert await asyncio.wait_for(reader.read(), 1) == "fresh"

    asyncio.run(scenario())


def test_picker_error_is_safe_and_does_not_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    picker = AsyncMock(side_effect=RuntimeError("fake-provider-secret"))
    select = Mock()
    manager = ModelManager(
        ProviderConfig("https://example.invalid", "FAKE_KEY", GROUPS)
    )
    context = CommandContext(
        trace_store=TraceStore(tmp_path),
        model_manager=manager,
        active_provider_name="local",
        choose_model=picker,
        select_model=select,
    )
    result = CommandRouter().handle("/model", context)

    async def interact() -> Action | None:
        assert result.interaction is not None
        return await result.interaction()

    action = asyncio.run(interact())
    # The refusal is the interaction's return value now: the chooser failed, so the
    # surface is told why without the credential-bearing diagnostic reaching it.
    assert action is not None
    assert "Could not open model selector" in action.message
    assert "fake-provider-secret" not in action.message
    select.assert_not_called()
    assert manager.current_model() == GROUPS[0]


def test_non_tty_summary_and_ordered_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    picker = AsyncMock()
    select = Mock()
    context = CommandContext(
        trace_store=TraceStore(tmp_path),
        model_manager=ModelManager(
            ProviderConfig("https://example.invalid", "FAKE_KEY", GROUPS)
        ),
        active_provider_name="local",
        choose_model=picker,
        select_model=select,
    )
    router = CommandRouter()
    result = router.handle("/model", context)
    assert result.handled and result.interaction is None
    assert result.action is not None
    summary = result.action.message
    assert "Current group: flash (primary: openai/flash) | provider: local" in summary
    assert "Fallbacks: openai/backup -> openai/last" in summary
    assert "/model list" in summary and "/model use <group>" in summary
    picker.assert_not_called()
    select.assert_not_called()
    router.handle("/model list", context)
    listing = capsys.readouterr().out
    assert "Provider: local" in listing
    assert "* flash\n    1. openai/flash — primary" in listing
    assert "2. openai/backup — fallback 1" in listing
    assert "3. openai/last — fallback 2" in listing
    assert (
        listing.index("openai/flash")
        < listing.index("openai/backup")
        < listing.index("openai/last")
    )


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("switch", "plus"),
        ("current", "flash"),
        ("failure", "flash"),
    ],
)
def test_choosing_a_group_preserves_session_and_provider_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    expected: str,
) -> None:
    """Selecting a model group changes the model, and nothing else.

    The chooser is the bottom action surface now, and confirming a row means
    ``/model use <name>`` — so this drives the same command the row runs and asserts
    the boundary that makes a switch safe: the Agent object, its state, its tools,
    its grants and its conversation all survive, and only the model changes.
    """
    state = configure_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    state.handler.grants.add(PermissionCapability.NETWORK)
    original_counter = counter_module.LiteLLMTokenCounter

    def make_counter(model: str) -> counter_module.LiteLLMTokenCounter:
        if outcome == "failure" and model == "openai/qwen-plus":
            # Construction must not publish selection before it succeeds.
            assert state.contexts[0].model_manager.current_model().name == "flash"
            raise RuntimeError(f"provider diagnostic: {BAILIAN_CREDENTIAL}")
        return original_counter(model)

    monkeypatch.setattr(counter_module, "LiteLLMTokenCounter", make_counter)
    typed = "/model use flash\r" if outcome == "current" else "/model use plus\r"

    async def scenario() -> None:
        ready: asyncio.Queue[str] = asyncio.Queue()

        class ObservedInput(CliInput):
            async def read(self) -> str:
                ready.put_nowait("prompt")
                return await super().read()

        async def expect(kind: str) -> None:
            assert await asyncio.wait_for(ready.get(), 3) == kind

        with create_pipe_input() as pipe:
            reader = ObservedInput(input=pipe, output=DummyOutput())
            task = asyncio.create_task(cli_module.main(cli_input=reader))
            try:
                await expect("prompt")
                pipe.send_text("first question\r")
                await expect("prompt")
                agent = state.agents[0]
                before = (
                    agent,
                    agent.state,
                    agent.tools,
                    agent.permission_handler,
                    agent.tracer,
                    agent.model_executor,
                )
                runtime = (agent.llm, agent.context_builder)
                budget = agent.context_builder.budget
                history = list(agent.state.messages)
                context = state.contexts[0]
                select = Mock(wraps=context.select_model)
                context.select_model = select
                pipe.send_text(typed)
                await expect("prompt")
                after = (
                    agent,
                    agent.state,
                    agent.tools,
                    agent.permission_handler,
                    agent.tracer,
                    agent.model_executor,
                )
                assert all(a is b for a, b in zip(before, after, strict=True))
                assert agent.context_builder.budget is budget
                assert agent.state.messages == history
                assert state.handler.grants == {
                    PermissionCapability.NETWORK,
                    PermissionCapability.WORKSPACE_TRUST,
                }
                assert context.active_provider_name == "bailian"
                assert state.resolved_keys == ["BAILIAN_API_KEY"]
                assert state.approvals == [("bailian", "flash", False)]
                assert context.model_manager.current_model().name == expected
                if outcome == "current":
                    # Selecting the group already in use is not a switch at all.
                    select.assert_not_called()
                else:
                    # A switch is attempted; on failure the runtime rolls back.
                    select.assert_called_once_with("plus")
                if outcome != "switch":
                    assert agent.llm is runtime[0]
                    assert agent.context_builder is runtime[1]
                pipe.send_text("second question\r")
                await expect("prompt")
                pipe.send_text("/exit\r")
                await asyncio.wait_for(task, 3)
                assert len(state.agents) == 1
                assert [request["model"] for request in state.requests] == [
                    "openai/qwen-flash",
                    f"openai/qwen-{expected}",
                ]
                assert all(
                    request["api_key"] == BAILIAN_CREDENTIAL
                    for request in state.requests
                )
                assert [message.content for message in agent.state.messages] == [
                    "first question",
                    "reply 1",
                    "second question",
                    "reply 2",
                ]
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert BAILIAN_CREDENTIAL not in output
    assert "provider diagnostic" not in output
    if outcome == "failure":
        assert "Could not switch model group" in output


def test_confirming_a_chooser_row_switches_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The menu's rows must reach the same command the typed form does.

    A chooser shows group names; what turns a name into a switch is the ``/model
    use`` command it stands for. If the surface forgets which command opened it, the
    cursor still moves and the details still update, but confirming does nothing —
    a menu that looks alive and is not.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    manager = ModelManager(
        ProviderConfig("https://example.invalid", "FAKE_KEY", GROUPS)
    )
    switched: list[str] = []

    def select(name: str) -> ModelConfig:
        switched.append(name)
        for group in GROUPS:
            if group.name == name:
                return group
        raise AssertionError(name)

    context = CommandContext(
        trace_store=TraceStore(tmp_path),
        model_manager=manager,
        active_provider_name="local",
        select_model=select,
        has_action_surface=True,
    )
    router = CommandRouter()
    chooser = router.handle("/model", context).action
    assert chooser is not None and chooser.choosable

    # Confirm the second row, exactly as Enter on the surface does.
    row = chooser.rows[1]
    result = router.handle(f"/model use {row.name}", context)

    assert switched == [row.name]
    assert result.action is not None
    assert "Current group" in result.action.message
