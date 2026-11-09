import sys
from io import StringIO
from typing import Any

import pytest
from rich.console import Console

import cairn.interfaces.cli.output as output_module


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


def test_stream_display_does_not_interpret_emoji_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.agent.turn.events import Event

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
    from cairn.interfaces.cli.output import StreamingDisplay

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
    from cairn.agent.turn.events import Event

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
    from cairn.agent.turn.events import Event

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
    from cairn.agent.turn.events import Event

    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    for thought in ("one", "two", "three", "four"):
        display.observe(
            Event(type="model_reasoning_delta", data={"text": f"{thought}\n"})
        )

    shown = [line for line in log.getvalue().splitlines()]
    assert shown == [f"⋮ {word}" for word in ("one", "two", "three", "four")]


def test_reset_closes_a_live_region_left_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cancelled attempt can leave the terminal's live region running. The next
    # turn's reset must stop it, or a region nobody owns keeps repainting.
    from cairn.agent.turn.events import Event

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
    from cairn.agent.turn.events import Event

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
    from cairn.interfaces.cli.output import _split_blocks

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
    # handler saw it, so the display never learned the turn's trace and its totals.
    from cairn.agent.turn.events import Event

    _answer, _log, display = _plain_display(monkeypatch)

    handled = display.observe(
        Event(type="trace_finish", data={"trace_id": "abc123def456", "persisted": True})
    )

    assert handled
    # The identifier is recorded, not printed: `/trace` is what reads it back, and
    # the two status rows are spent on the model, the provider and the numbers.
    assert display._trace_id == "abc123def456"


def test_a_failed_trace_says_nothing_about_absent_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A turn that measured no usage has nothing to report about tokens; the
    # unknown-input/unknown-output suffix belonged to no real measurement.
    from cairn.agent.turn.events import Event

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
    from cairn.agent.turn.events import Event

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
    from cairn.agent.turn.events import Event

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
    from cairn.agent.turn.events import Event

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
    from cairn.agent.turn.events import Event

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
    from cairn.agent.turn.events import Event

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
