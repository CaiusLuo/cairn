import asyncio
import sys
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


def isolate_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[StringIO, StringIO]:
    """Put both output channels of a redirected run in memory.

    A redirected turn writes settled blocks through the console and reasoning
    as plain lines on stdout, because no live region will ever repaint them.
    """
    answer = StringIO()
    reasoning = StringIO()
    monkeypatch.setattr(
        output_module,
        "console",
        Console(file=answer, width=200, force_terminal=False, color_system=None),
    )
    monkeypatch.setattr(sys, "stdout", reasoning)
    return answer, reasoning


def configure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> tuple[StringIO, list[Agent], StringIO]:
    """Configure a streaming CLI turn; the third item is its reasoning log."""
    for key, value in {
        "CAIRN_LLM_MODEL": "openai/test",
        "CAIRN_LLM_API_KEY": "TOPSECRET",
        "CAIRN_BASE_URL": "https://example.test/v1",
    }.items():
        monkeypatch.setenv(key, value)
    isolate_cli(tmp_path, monkeypatch, "hello")
    set_limits(monkeypatch)
    monkeypatch.setattr(counter_module, "token_counter", lambda **kwargs: 10)
    text, reasoning = isolate_stream(monkeypatch)
    agents: list[Agent] = []

    async def capture(agent: Agent, user_input: str, *, budget: RunBudget) -> str:
        agents.append(agent)
        return await run_turn(agent, user_input, budget=budget)

    monkeypatch.setattr(cli_module, "run_turn", capture)
    return text, agents, reasoning


def test_real_cli_deltas_visible_before_completion_plain_and_not_duplicated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    text, agents, notes = configure(monkeypatch, tmp_path)

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
        # Reasoning streams visibly as plain lines; the answer is still being
        # assembled and arrives as one Markdown block when the attempt settles.
        assert "先思考" in notes.getvalue()
        assert "⋮" in notes.getvalue()
        assert "\x1b" not in notes.getvalue()
        assert "你好" not in notes.getvalue()
        assert not task.done()
        assert [message.role for message in agents[0].state.messages] == ["user"]
        blocked.release.set()
        await task
        assert text.getvalue().count("你好") == 1
        assert " 🌳" in text.getvalue()
        assert text.getvalue().index("🌳") < text.getvalue().index("✓ done")
        assert "trace:" not in text.getvalue()
        assert (
            agents[0].state.messages[-1].content
            == "你好 [bold]literal[/bold] \x1b[31mred\x1b[0m 🌳"
        )
        assert stream.closed

    asyncio.run(scenario())


def test_public_cli_partial_fallback_labels_abandoned_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    text, agents, _notes = configure(monkeypatch, tmp_path)
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
    # Prose goes through the console channel; reasoning is plain lines, which
    # CliRunner collects into the command's own output. A thought reaches a
    # redirected log twice by design: once while it is still growing (padded and
    # rewritten in place) and once settled. What must not happen is a repeat of
    # the settled form.
    reasoning_shown = result.output
    assert reasoning_shown.count("⋮ ACCEPTED_THOUGHT\n") == 1
    # The abandoned attempt's reasoning is already on screen before the label
    # that explains why its answer was dropped, and that answer never prints.
    assert "ABANDONED_THOUGHT" in reasoning_shown
    assert "[attempt abandoned; incomplete output discarded]" in shown
    assert "ABANDONED\n" not in shown
    assert "ACCEPTED" in shown
    assert [line.strip() for line in shown.splitlines()].count("ACCEPTED") == 1
    assert "TOPSECRET" not in shown
    assert calls == ["openai/one", "openai/two"]
    assert agents[0].state.messages[-1].content == "ACCEPTED"
    assert first.closed and second.closed


def test_cli_cancellation_labels_incomplete_and_never_previews_tool_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    text, agents, notes = configure(monkeypatch, tmp_path)

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
        assert "[response incomplete; incomplete output discarded]" in text.getvalue()
        # Reasoning that already reached the screen stays there: a cancelled
        # attempt discards its answer, not what the reader has already seen.
        assert "unfinished thought" in notes.getvalue()
        assert "TOPSECRET" not in text.getvalue()
        assert "TOPSECRET" not in notes.getvalue()
        assert "Permission required" not in text.getvalue()
        assert agents[0].state.messages == []
        assert stream.closed

    asyncio.run(scenario())


def test_cli_generate_only_client_keeps_markdown_rendering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    text, agents, _notes = configure(monkeypatch, tmp_path)

    async def non_streaming(agent: Agent, user_input: str, *, budget: RunBudget) -> str:
        agents.append(agent)
        agent.llm = SequenceLLM([LLMResponse(content="**LEGACY**")])
        return await run_turn(agent, user_input, budget=budget)

    monkeypatch.setattr(cli_module, "run_turn", non_streaming)
    result = CliRunner().invoke(cli_module.app, [])
    assert result.exit_code == 0, result.output
    assert "LEGACY" in text.getvalue()
    assert "**LEGACY**" not in text.getvalue()
    assert "⋮" not in text.getvalue()
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


def _plain_display(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[StringIO, StringIO, Any]:
    """A StreamingDisplay over in-memory, non-terminal console and stdout."""
    from cairn.terminal.output import StreamingDisplay

    answer = StringIO()
    log = StringIO()
    monkeypatch.setattr(
        output_module,
        "console",
        Console(file=answer, width=200, color_system=None, force_terminal=False),
    )
    monkeypatch.setattr(sys, "stdout", log)
    return answer, log, StreamingDisplay()


def _feed(display: Any, chunks: list[str], *, step: int = 1) -> None:
    from cairn.core.events import Event

    display.observe(Event(type="model_stream_start", data={"step": step, "attempt": 1}))
    for chunk in chunks:
        display.observe(Event(type="model_text_delta", data={"text": chunk}))
    display.observe(
        Event(type="model_stream_end", data={"step": step, "status": "completed"})
    )


def test_open_code_fence_settles_once_instead_of_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fence still open when the stream ends prints its content exactly once;
    # parking the whole answer in the pending tail used to drop it entirely.
    # (Rich renders the code block itself, so no literal ``` fences survive.)
    text, _log, display = _plain_display(monkeypatch)
    _feed(display, ["intro\n\n```python\nprint('hi')\n"])
    shown = text.getvalue()
    assert "intro" in shown
    assert shown.count("print('hi')") == 1
    assert display.final_text_visible


def test_reasoning_streams_without_cursor_escape_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reasoning redraws go through the live region, which owns cursor math;
    # nothing hand-moves the cursor anymore, so a repaint cannot erase lines
    # that belong to output printed above the reasoning block.
    text, log, display = _plain_display(monkeypatch)
    from cairn.core.events import Event

    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    for thought in ("plan the change\n", "revise the plan\n"):
        display.observe(Event(type="model_reasoning_delta", data={"text": thought}))
    display.observe(
        Event(type="model_stream_end", data={"step": 1, "status": "completed"})
    )
    shown = log.getvalue()
    assert "plan the change" in shown
    assert "revise the plan" in shown
    assert shown.index("plan the change") < shown.index("revise the plan")
    assert "\x1b[" not in text.getvalue()
    assert "\x1b[" not in shown


def test_reasoning_is_appended_once_per_line_off_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A redirected stream has no cursor to redraw, so each line is written when
    # it becomes a line and never again. Reprinting the whole trailing window on
    # every delta turned four thoughts into ten lines with repeats.
    _answer, log, display = _plain_display(monkeypatch)
    from cairn.core.events import Event

    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    for thought in ("one", "two", "three", "four"):
        display.observe(
            Event(type="model_reasoning_delta", data={"text": f"{thought}\n"})
        )

    shown = [line for line in log.getvalue().splitlines()]
    assert shown == [f"⋮ {word}" for word in ("one", "two", "three", "four")]


def test_reasoning_window_fits_the_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The trailing window is a share of the screen rather than a fixed count, so
    # a short terminal keeps the answer visible while the model thinks.
    import cairn.terminal.output as output

    monkeypatch.setattr(output.console, "height", 16, raising=False)
    assert output._reasoning_window() == 4
    monkeypatch.setattr(output.console, "height", 8, raising=False)
    assert output._reasoning_window() == output.MIN_THINKING_LINES
    monkeypatch.setattr(output.console, "height", 80, raising=False)
    assert output._reasoning_window() == output.MAX_THINKING_LINES


def test_reset_closes_a_live_region_left_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cancelled attempt can leave the terminal's live region running. The next
    # turn's reset must stop it, or a region nobody owns keeps repainting.
    from cairn.core.events import Event

    text, _log, display = _plain_display(monkeypatch)
    stopped: list[bool] = []

    class _RecordingLive:
        def start(self) -> None:
            stopped.append(False)

        def update(self, renderable: Any) -> None:
            stopped.append(False)

        def stop(self) -> None:
            stopped.append(True)

    monkeypatch.setattr(output_module, "supports_live_display", lambda: True)
    monkeypatch.setattr(output_module, "Live", lambda *args, **kwargs: _RecordingLive())
    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    display.observe(Event(type="model_reasoning_delta", data={"text": "thinking"}))
    assert stopped.count(True) == 0

    display.reset()

    assert stopped[-1] is True
    assert text.getvalue() == ""


def test_reset_clears_stream_state_between_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reasoning shown before reset is already on screen (off-terminal each
    # delta prints once); reset must keep it from leaking into the *next*
    # turn's display state, so the next answer renders alone.
    text, log, display = _plain_display(monkeypatch)
    from cairn.core.events import Event

    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    display.observe(Event(type="model_reasoning_delta", data={"text": "STALE\n"}))
    display.reset()
    _feed(display, ["FRESH"], step=2)
    reasoning_shown = log.getvalue()
    assert reasoning_shown.count("STALE") == 1
    assert "STALE" not in reasoning_shown.partition("STALE")[2]
    assert text.getvalue().count("FRESH") == 1
    assert "FRESH" not in reasoning_shown


def test_a_closed_code_fence_settles_mid_stream_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure this guards: an open fence used to park the whole answer, so
    # prose before the block froze and everything landed at once at stream end.
    # A fence that closes mid-stream must print once, in the answer's order.
    text, _log, display = _plain_display(monkeypatch)
    _feed(display, ["Here is the code:\n\n```python\nprint('hi')\n```\n", "\nAfter.\n"])

    shown = text.getvalue()
    assert shown.count("print('hi')") == 1
    assert shown.index("Here is the code") < shown.index("print('hi')")
    assert shown.index("print('hi')") < shown.index("After.")


def test_open_fence_keeps_settling_the_prose_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # While a fence stays open, completed prose above it is still printable:
    # only the fence itself waits in the pending tail.
    from cairn.terminal.output import _split_blocks

    settled, pending = _split_blocks("Prose.\n\n```py\nx = 1\n")
    assert settled == "Prose.\n\n"
    assert pending == "```py\nx = 1\n"
    assert _split_blocks("Prose.\n\n```py\nx = 1\n```\n\nAfter\n")[0].endswith(
        "```\n\n"
    )


def test_trace_finish_reaches_the_display_and_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The CLI used to swallow trace_finish before the display and the console
    # handler saw it, so the status line never learned the turn's trace and
    # `/trace` always answered "No trace available yet.".
    from cairn.core.events import Event

    _answer, _log, display = _plain_display(monkeypatch)

    handled = display.observe(
        Event(type="trace_finish", data={"trace_id": "abc123def456", "persisted": True})
    )

    assert handled
    assert "abc123de" in display._status_line().plain


def test_a_failed_trace_says_nothing_about_absent_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A turn that measured no usage has nothing to report about tokens; the
    # unknown-input/unknown-output suffix belonged to no real measurement.
    from cairn.core.events import Event

    text = StringIO()
    monkeypatch.setattr(
        output_module,
        "console",
        Console(file=text, width=200, color_system=None, force_terminal=False),
    )
    output_module.console_event_handler(
        Event(
            type="trace_finish",
            data={"trace_id": "x", "persisted": False, "persistence_error": "OSError"},
        )
    )

    assert "trace unavailable: persistence failed (OSError)" in text.getvalue()
    assert "tokens:" not in text.getvalue()


def test_a_durable_turn_reports_a_failed_trace_in_its_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Durable mode prints its own "Trace unavailable" line at the turn boundary;
    # the console handler must not add a second one.
    from cairn.core.events import Event

    text = StringIO()
    monkeypatch.setattr(
        output_module,
        "console",
        Console(file=text, width=200, color_system=None, force_terminal=False),
    )
    output_module.console_event_handler(
        Event(
            type="trace_finish",
            data={"trace_id": "x", "persisted": False, "persistence_error": "OSError"},
        ),
        report_trace_failure=False,
    )

    assert text.getvalue() == ""


def test_an_aborted_attempt_keeps_the_log_free_of_blank_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A redirected stream ends its reasoning with a newline already, and its
    # warning goes to the console, so a separating blank line would only add
    # noise to a log a reader is scanning for evidence.
    from cairn.core.events import Event

    _answer, log, display = _plain_display(monkeypatch)
    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    display.observe(Event(type="model_reasoning_delta", data={"text": "cut short\n"}))

    display.close_incomplete()

    assert log.getvalue() == "⋮ cut short\n"


def test_an_aborted_attempt_separates_the_warning_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a terminal the reasoning may still be the last thing on screen, so the
    # warning needs one blank line between it and what precedes it.
    from cairn.core.events import Event

    monkeypatch.setattr(output_module, "supports_live_display", lambda: True)
    text = StringIO()
    monkeypatch.setattr(
        output_module,
        "console",
        Console(file=text, width=80, color_system=None, force_terminal=False),
    )
    monkeypatch.setattr(sys, "stdout", StringIO())
    display = output_module.StreamingDisplay()
    recorded: list[bool] = []

    class _RecordingLive:
        def start(self) -> None:
            recorded.append(True)

        def update(self, renderable: Any) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(output_module, "Live", lambda *args, **kwargs: _RecordingLive())
    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    display.observe(Event(type="model_reasoning_delta", data={"text": "cut short\n"}))

    display.close_incomplete()

    assert recorded
    assert text.getvalue().startswith("\n")
    assert "[response incomplete; incomplete output discarded]" in text.getvalue()


def test_the_abort_paths_own_disjoint_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `_discard_attempt_output` drops what the attempt produced and keeps what
    # the turn knows; `_forget_attempt` drops both. A cancel must not forget the
    # completed step the caller still reports.
    from cairn.core.events import Event

    _answer, _log, display = _plain_display(monkeypatch)
    display.observe(Event(type="model_stream_start", data={"step": 3, "attempt": 1}))
    display.observe(Event(type="model_text_delta", data={"text": "partial\n"}))
    display.observe(
        Event(type="model_stream_end", data={"step": 3, "status": "incomplete"})
    )

    assert display._answer == [] and display._reasoning == []
    assert not display._active


def test_logging_reasoning_does_not_rescan_the_whole_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A delta used to rewrap every settled line, so a long chain of thought cost
    # quadratic time on the log path. Counting the lines the writer touches is a
    # deterministic stand-in for that: each delta must add work for its own line
    # only.
    from cairn.core.events import Event

    _answer, _log, display = _plain_display(monkeypatch)
    calls: list[int] = []
    original = output_module._rows_for_lines

    def counting(lines: list[str], *, growing: bool) -> list[tuple[str, bool]]:
        calls.append(len(lines))
        return original(lines, growing=growing)

    monkeypatch.setattr(output_module, "_rows_for_lines", counting)
    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    for index in range(40):
        display.observe(
            Event(type="model_reasoning_delta", data={"text": f"thought {index}\n"})
        )

    assert max(calls) == 1, calls
