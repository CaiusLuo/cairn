import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import cairn.cli as cli_module
import cairn.llm.token_counter as counter_module
from cairn.core.permissions import PermissionCapability
from cairn.llm.model_manager import ModelConfig, ModelManager, ProviderConfig
from cairn.observability.storage import TraceStore
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.router import CommandRouter
from cairn.terminal.input import CliInput
from tests.cli.test_provider_session import BAILIAN_CREDENTIAL, configure_cli

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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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

    async def interact() -> None:
        assert result.interaction is not None
        await result.interaction()

    asyncio.run(interact())
    output = capsys.readouterr().out
    assert "Could not open model selector" in output
    assert "fake-provider-secret" not in output
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
    summary = capsys.readouterr().out
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
    "outcome", ["switch", "current", "escape", "interrupt", "failure"]
)
def test_picker_in_real_cli_preserves_session_and_provider_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
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
    keys = {
        "switch": "\x1b[B\r",
        "current": "\r",
        "escape": "\x1b",
        "interrupt": "\x03",
        "failure": "\x1b[B\r",
    }

    async def scenario() -> None:
        ready: asyncio.Queue[str] = asyncio.Queue()

        class ObservedInput(CliInput):
            async def read(self) -> str:
                ready.put_nowait("prompt")
                return await super().read()

            async def choose_model(
                self, provider: str, models: tuple[ModelConfig, ...], current: str
            ) -> str | None:
                ready.put_nowait("picker")
                return await super().choose_model(provider, models, current)

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
                pipe.send_text("/model\r")
                await expect("picker")
                pipe.send_text(keys[outcome])
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
                assert state.handler.grants == {PermissionCapability.NETWORK}
                assert context.active_provider_name == "bailian"
                assert state.resolved_keys == ["BAILIAN_API_KEY"]
                assert state.approvals == [("bailian", "flash", False)]
                expected = "plus" if outcome == "switch" else "flash"
                assert context.model_manager.current_model().name == expected
                if outcome in {"switch", "failure"}:
                    select.assert_called_once_with("plus")
                else:
                    select.assert_not_called()
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
    if outcome in {"escape", "interrupt"}:
        assert "Model selection cancelled" in output
