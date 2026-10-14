import asyncio
from io import StringIO
from typing import Any

import pytest
from litellm.exceptions import RateLimitError
from rich.console import Console
from typer.testing import CliRunner

import cairn.infra.llm.litellm_client as llm_module
import cairn.infra.llm.token_counter as counter_module
import cairn.terminal.cli as cli_module
import cairn.terminal.output as output_module
from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from tests.support.cli_harness import isolate_cli, set_limits
from tests.support.runtime import SequenceLLM
from tests.support.streaming import ProviderStream, StreamBarrier, provider_chunk


def configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> tuple[StringIO, list[Agent]]:
    for key, value in {
        "CAIRN_LLM_MODEL": "openai/test",
        "CAIRN_LLM_API_KEY": "TOPSECRET",
        "CAIRN_BASE_URL": "https://example.test/v1",
    }.items():
        monkeypatch.setenv(key, value)
    isolate_cli(tmp_path, monkeypatch, "hello")
    set_limits(monkeypatch)
    monkeypatch.setattr(counter_module, "token_counter", lambda **kwargs: 10)
    text = StringIO()
    monkeypatch.setattr(
        output_module,
        "console",
        Console(file=text, width=200, force_terminal=False, color_system=None),
    )
    agents: list[Agent] = []

    async def capture(agent: Agent, user_input: str, *, budget: RunBudget) -> str:
        agents.append(agent)
        return await run_turn(agent, user_input, budget=budget)

    monkeypatch.setattr(cli_module, "run_turn", capture)
    return text, agents


def test_real_cli_deltas_visible_before_completion_plain_and_not_duplicated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    text, agents = configure(monkeypatch, tmp_path)

    async def scenario() -> None:
        blocked = StreamBarrier(asyncio.Event(), asyncio.Event())
        stream = ProviderStream(
            provider_chunk(reasoning="先思考 [bold]literal[/bold] \x1b[31m"),
            provider_chunk("你好 [bold]literal[/bold] \x1b[31mred\x1b[0m"),
            blocked,
            provider_chunk(" 🌳"),
            provider_chunk(finish="stop"),
            {"choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 3}},
        )

        async def completion(**kwargs: Any) -> ProviderStream:
            assert kwargs["stream"] is True
            return stream

        monkeypatch.setattr(llm_module, "acompletion", completion)
        task = asyncio.create_task(cli_module.main())
        await blocked.entered.wait()
        shown = text.getvalue()
        assert "你好 [bold]literal[/bold] [31mred[0m" in shown
        assert "(thinking, provisional)" in shown
        assert shown.index("先思考") < shown.index("你好")
        assert "\x1b" not in shown and "(provisional)" in shown
        assert not task.done()
        assert [message.role for message in agents[0].state.messages] == ["user"]
        blocked.release.set()
        await task
        assert text.getvalue().count("你好") == 1
        assert " 🌳" in text.getvalue()
        assert "tokens: input 17, output 3" in text.getvalue()
        assert (
            text.getvalue().index("🌳")
            < text.getvalue().index("✓ done")
            < text.getvalue().index("trace:")
        )
        assert (
            agents[0].state.messages[-1].content
            == "你好 [bold]literal[/bold] \x1b[31mred\x1b[0m 🌳"
        )
        assert stream.closed

    asyncio.run(scenario())


def test_public_cli_partial_fallback_labels_abandoned_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    text, agents = configure(monkeypatch, tmp_path)
    (tmp_path / ".cairn").mkdir()
    (tmp_path / ".cairn/models.toml").write_text(
        'base_url = "https://example.test/v1"\napi_key_env = "CAIRN_LLM_API_KEY"\n[[models]]\nname = "chat"\nmodel_ids = ["openai/one", "openai/two"]\n'
    )
    monkeypatch.setattr(
        cli_module, "confirm_provider_access", lambda *args, **kwargs: True
    )
    first = ProviderStream(
        provider_chunk(reasoning="ABANDONED_THOUGHT"),
        provider_chunk("ABANDONED"),
        RateLimitError(message="throttled", model="one", llm_provider="openai"),
    )
    second = ProviderStream(
        provider_chunk(reasoning="ACCEPTED_THOUGHT"),
        provider_chunk("ACCEPTED"),
        provider_chunk(finish="stop"),
    )
    calls: list[str] = []

    async def completion(**kwargs: Any) -> ProviderStream:
        calls.append(kwargs["model"])
        assert first.closed or len(calls) == 1
        return first if len(calls) == 1 else second

    monkeypatch.setattr(llm_module, "acompletion", completion)
    result = CliRunner().invoke(cli_module.app, [])
    assert result.exit_code == 0, result.output
    shown = text.getvalue()
    assert (
        shown.index("ABANDONED")
        < shown.index("[abandoned attempt; provisional output discarded]")
        < shown.index("ACCEPTED")
    )
    assert shown.count("ACCEPTED_THOUGHT") == 1
    assert shown.count("ACCEPTED\n") == 1
    assert "TOPSECRET" not in shown
    assert calls == ["openai/one", "openai/two"]
    assert agents[0].state.messages[-1].content == "ACCEPTED"
    assert first.closed and second.closed


def test_cli_cancellation_labels_incomplete_and_never_previews_tool_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    text, agents = configure(monkeypatch, tmp_path)

    async def scenario() -> None:
        blocked = StreamBarrier(asyncio.Event(), asyncio.Event())
        stream = ProviderStream(
            provider_chunk(reasoning="unfinished thought"),
            provider_chunk("partial"),
            provider_chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"TOPSECRET',
                        },
                    }
                ]
            ),
            blocked,
        )

        async def completion(**kwargs: Any) -> ProviderStream:
            return stream

        monkeypatch.setattr(llm_module, "acompletion", completion)
        task = asyncio.create_task(cli_module.main())
        await blocked.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "[incomplete response; provisional output discarded]" in text.getvalue()
        assert "unfinished thought" in text.getvalue()
        assert "TOPSECRET" not in text.getvalue()
        assert "Permission required" not in text.getvalue()
        assert agents[0].state.messages == []
        assert stream.closed

    asyncio.run(scenario())


def test_cli_generate_only_client_keeps_markdown_rendering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    text, agents = configure(monkeypatch, tmp_path)

    async def non_streaming(agent: Agent, user_input: str, *, budget: RunBudget) -> str:
        agents.append(agent)
        agent.llm = SequenceLLM([LLMResponse(content="**LEGACY**")])
        return await run_turn(agent, user_input, budget=budget)

    monkeypatch.setattr(cli_module, "run_turn", non_streaming)
    result = CliRunner().invoke(cli_module.app, [])
    assert result.exit_code == 0, result.output
    assert "LEGACY" in text.getvalue()
    assert "**LEGACY**" not in text.getvalue()
    assert "(provisional)" not in text.getvalue()
    assert agents[0].state.messages[-1].content == "**LEGACY**"


def test_stream_display_does_not_interpret_emoji_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.core.events import Event

    text = StringIO()
    monkeypatch.setattr(
        output_module,
        "console",
        Console(file=text, color_system=None, force_terminal=False),
    )
    display = output_module.StreamingDisplay()
    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    display.observe(
        Event(
            type="model_text_delta",
            data={"text": ":rocket: [link=https://example.test]literal[/link]\r\b\x9b"},
        )
    )
    display.observe(
        Event(type="model_stream_end", data={"step": 1, "status": "completed"})
    )
    assert ":rocket: [link=https://example.test]literal[/link]" in text.getvalue()
    assert not any(control in text.getvalue() for control in ("\r", "\b", "\x9b"))
