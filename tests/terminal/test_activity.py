"""The activity surface: one answer to "what is happening now".

Thinking, tool work and an interrupt used to arrive through three routes, so the
reader looked in two places for one answer. These tests pin the single surface and
the rule that makes it safe: nothing it holds is printed, so a turn's transient work
cannot leak into the transcript.

The second half covers the turn's lifecycle, which is what a reader notices:
reasoning leaves the screen when the answer starts, and its one duration line is
printed above the answer, once. Failures are asserted separately, because
hiding routine progress must never hide a denied call or a failed command.
"""

import io
from collections.abc import Callable
from typing import Literal, cast

import pytest
from rich.console import Console, RenderableType
from rich.text import Text

from cairn.core.events import Event
from cairn.terminal import output as ui
from cairn.terminal.activity import Activity, ActivityKind
from cairn.terminal.output import StreamingDisplay


def test_nothing_is_happening_at_first() -> None:
    activity = Activity()
    assert activity.kind is ActivityKind.IDLE
    assert not activity.busy


def test_thinking_is_an_activity() -> None:
    activity = Activity()
    activity.thinking()

    assert activity.kind is ActivityKind.THINKING
    assert activity.busy


def test_running_a_tool_names_it() -> None:
    activity = Activity()
    activity.working("read_file a.py")

    assert activity.kind is ActivityKind.WORKING
    assert "read_file" in activity.detail


def test_an_interrupt_is_an_activity() -> None:
    activity = Activity()
    activity.interrupting()

    assert activity.kind is ActivityKind.INTERRUPTING
    assert activity.busy


def test_only_one_activity_is_current() -> None:
    """A list of activities would grow into a second transcript."""
    activity = Activity()
    activity.thinking()
    activity.working("grep foo")

    assert activity.kind is ActivityKind.WORKING


def test_a_finished_tool_returns_to_thinking() -> None:
    """The turn is still going; it is just not inside a tool any more."""
    activity = Activity()
    activity.working("read_file a.py")
    activity.finished_tool()

    assert activity.kind is ActivityKind.THINKING


def test_finished_calls_are_counted() -> None:
    activity = Activity()
    activity.finished_tool()
    activity.finished_tool()

    assert activity.calls == 2


def test_the_summary_names_the_call_count() -> None:
    activity = Activity()
    for _ in range(3):
        activity.finished_tool()

    assert activity.summary() == "3 tool calls"


def test_one_call_is_singular() -> None:
    activity = Activity()
    activity.finished_tool()

    assert activity.summary() == "1 tool call"


def test_a_turn_without_tools_keeps_no_summary() -> None:
    """Saying "0 calls" is noise; saying nothing is accurate."""
    assert Activity().summary() == ""


def test_going_idle_clears_the_detail() -> None:
    activity = Activity()
    activity.working("read_file a.py")
    activity.idle()

    assert not activity.busy
    assert activity.detail == ""


def test_resetting_clears_the_count() -> None:
    activity = Activity()
    activity.finished_tool()
    activity.reset()

    assert activity.calls == 0
    assert activity.summary() == ""
    assert not activity.busy


def test_an_interrupt_survives_a_finished_tool() -> None:
    """A tool that lands while stopping must not hide that we are stopping."""
    activity = Activity()
    activity.interrupting()
    activity.finished_tool()

    assert activity.kind is ActivityKind.INTERRUPTING


# -- ownership ------------------------------------------------------------
#
# The display owns tool events outright: it shows the call on the activity surface
# and prints nothing. What a reader can audit afterwards is the trace, not a line
# per call in the conversation.


def _display() -> StreamingDisplay:
    display = StreamingDisplay()
    display.reset()
    display._reasoning_open = True
    return display


def _surface(display: StreamingDisplay) -> str:
    """The activity surface as plain text, narrowed once for every assertion."""
    rendered = display._activity_renderable()
    return rendered.plain if isinstance(rendered, Text) else ""


def _call(label: str = "read_file a.py") -> Event:
    return Event(
        type="tool_call", data={"tool": "read_file", "arguments": {"path": "a.py"}}
    )


def test_the_display_claims_a_tool_call() -> None:
    """Claiming it is what keeps the console handler from printing it."""
    display = _display()
    assert display.observe(_call()) is True


def test_the_display_claims_a_tool_result() -> None:
    display = _display()
    result = Event(
        type="tool_result",
        data={"tool": "read_file", "exit_code": 0, "stdout": "x", "tool_call_id": "c"},
    )
    assert display.observe(result) is True


def test_a_running_tool_is_named_on_the_surface() -> None:
    display = _display()
    display.observe(_call())

    assert "read_file" in _surface(display)


def test_a_finished_tool_leaves_the_surface() -> None:
    """The surface is present tense; once the call is done it stops saying so."""
    display = _display()
    display.observe(_call())
    display.observe(
        Event(
            type="tool_result",
            data={
                "tool": "read_file",
                "exit_code": 0,
                "stdout": "x",
                "tool_call_id": "c",
            },
        )
    )

    assert "read_file" not in _surface(display)


def test_a_tool_result_is_never_written_to_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool output is transient: the trace keeps the facts, the transcript does not."""
    transcript = io.StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=transcript, color_system=None, force_terminal=False, width=60),
    )
    display = _display()
    result = Event(
        type="tool_result",
        data={
            "tool": "bash",
            "exit_code": 0,
            "stdout": "TOOL-OUTPUT-MARKER",
            "tool_call_id": "c",
        },
    )

    display.observe(result)

    assert "TOOL-OUTPUT-MARKER" not in transcript.getvalue()
    assert transcript.getvalue().strip() == ""


def test_thinking_is_reported_by_the_same_surface() -> None:
    """Thinking must not be a second surface beside the activity one."""
    display = _display()
    display._thinking.begin(at=0.0)
    display._thinking.append("a thought")
    display._activity.thinking()

    surface = _surface(display)

    assert "Thinking" in surface
    assert "a thought" in surface


# -- the turn's lifecycle -------------------------------------------------
#
# These are the behaviours a reader actually notices: reasoning disappearing the
# moment the answer starts, and the one duration line that survives a turn. They
# are asserted against the printed transcript, not against the display's private
# fields, because the transcript is what the operator reads.


def _printed(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Capture everything the display prints into the scrollback."""
    transcript = io.StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=transcript, color_system=None, force_terminal=False, width=200),
    )
    return transcript


def _stream_delta(kind: str, text: str, *, step: int = 1) -> Event:
    # The runtime only ever emits these two for model output, so the union the
    # Event type carries is narrowed here rather than widened there.
    event_type = cast("Literal['model_text_delta', 'model_reasoning_delta']", kind)
    return Event(type=event_type, data={"text": text, "step": step})


def _stream_end(*, step: int = 1, status: str = "completed") -> Event:
    return Event(type="model_stream_end", data={"status": status, "step": step})


def _reasoning_display() -> StreamingDisplay:
    """A display mid-turn with reasoning received and no answer yet."""
    display = StreamingDisplay()
    display.observe(Event(type="model_stream_start", data={"step": 1}))
    display.observe(_stream_delta("model_reasoning_delta", "Reading the parser..."))
    return display


def test_thinking_is_cleared_the_moment_the_answer_starts() -> None:
    """The answer is the focus; the preview must not linger beside it."""
    display = _reasoning_display()
    assert "Thinking" in _surface(display)

    display.observe(_stream_delta("model_text_delta", "It splits tokens."))

    assert "Thinking" not in _surface(display)


def test_the_preview_stops_running_when_the_answer_starts() -> None:
    display = _reasoning_display()

    display.observe(_stream_delta("model_text_delta", "It splits tokens."))

    assert not display._thinking.running
    assert display._thinking.hint() == ""


def test_late_reasoning_does_not_reopen_the_preview() -> None:
    """A model that reasons again mid-turn has not reopened the question."""
    display = _reasoning_display()
    display.observe(_stream_delta("model_text_delta", "It splits tokens."))

    display.observe(_stream_delta("model_reasoning_delta", "second thoughts"))

    assert "Thinking" not in _surface(display)


def test_the_receipt_is_printed_before_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receipt freezes above the first visible answer token."""
    transcript = _printed(monkeypatch)
    display = _reasoning_display()

    display.observe(_stream_delta("model_text_delta", "It splits tokens."))
    display.observe(_stream_end())
    display.observe(Event(type="agent_finish", data={"step": 1}))

    rendered = transcript.getvalue()
    assert "It splits tokens." in rendered
    assert "Thinking" in rendered
    assert rendered.index("Thinking") < rendered.index("It splits tokens.")


def test_the_receipt_is_printed_once_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several reasoning steps are one act of thinking to a reader."""
    transcript = _printed(monkeypatch)
    display = _reasoning_display()
    display.observe(_stream_delta("model_text_delta", "first answer"))

    # A second reasoning step, then the stream settles.
    display.observe(_stream_delta("model_reasoning_delta", "more thought"))
    display.observe(_stream_delta("model_text_delta", "more answer"))
    display.observe(_stream_end())
    display.observe(Event(type="agent_finish", data={"step": 1}))

    assert transcript.getvalue().count("Thinking") == 1


def test_later_model_step_does_not_reopen_thinking_after_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step boundary cannot put a new preview after prose is visible."""
    transcript = _printed(monkeypatch)
    monkeypatch.setattr(ui, "supports_live_display", lambda: True)
    frames: list[str] = []

    class CapturedRegion:
        def _capture(self, renderable: RenderableType) -> None:
            output = io.StringIO()
            Console(
                file=output, color_system=None, force_terminal=False, width=80
            ).print(renderable)
            frames.append(output.getvalue())

        def start(self, renderable: RenderableType) -> None:
            self._capture(renderable)

        def update(self, renderable: RenderableType) -> None:
            self._capture(renderable)

        def flush(self) -> bool:
            return False

        def stop(self) -> None:
            pass

    def live_factory(_frame: Callable[[], RenderableType]) -> CapturedRegion:
        return CapturedRegion()

    display = StreamingDisplay(live_factory=live_factory)
    display.observe(Event(type="model_stream_start", data={"step": 1}))
    display.observe(_stream_delta("model_reasoning_delta", "first thought", step=1))
    display.observe(_stream_delta("model_text_delta", "first answer", step=1))
    display.observe(_stream_end(step=1))
    display.observe(_tool_call("between-steps"))
    display.observe(_tool_result("between-steps"))
    display.observe(Event(type="model_stream_start", data={"step": 2}))
    display.observe(_stream_delta("model_reasoning_delta", "second thought", step=2))
    display.observe(_stream_delta("model_text_delta", "second answer", step=2))
    display.observe(_stream_end(step=2))
    display.observe(Event(type="agent_finish", data={"step": 2}))

    rendered = transcript.getvalue()
    assert rendered.count("Thinking") == 1
    assert rendered.index("Thinking") < rendered.index("first answer")
    assert rendered.index("first answer") < rendered.index("second answer")
    first_answer_frame = next(
        index for index, frame in enumerate(frames) if "first answer" in frame
    )
    assert any("Thinking" in frame for frame in frames[:first_answer_frame])
    assert all("Thinking" not in frame for frame in frames[first_answer_frame:])
    assert all("second thought" not in frame for frame in frames)


def test_no_receipt_when_no_reasoning_arrived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that never reasoned must not claim a thinking step."""
    transcript = _printed(monkeypatch)
    display = StreamingDisplay()
    display.observe(Event(type="model_stream_start", data={"step": 1}))
    display.observe(_stream_delta("model_text_delta", "A direct answer."))
    display.observe(_stream_end())

    assert "Thinking" not in transcript.getvalue()


def test_a_turn_that_only_reasoned_still_reports_its_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = _printed(monkeypatch)
    display = _reasoning_display()

    display.observe(_stream_end())
    display.observe(Event(type="agent_finish", data={"step": 1}))

    assert "Thinking" in transcript.getvalue()


def test_tool_only_steps_keep_one_receipt_for_the_completed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = _printed(monkeypatch)
    display = StreamingDisplay()
    times = iter((0.0, 10.0, 12.0, 0.0, 101.0, 104.0))
    monkeypatch.setattr(ui, "perf_counter", lambda: next(times))

    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    display.observe(_stream_delta("model_reasoning_delta", "first thought", step=1))
    display.observe(_stream_end(step=1))
    assert "Thinking" not in transcript.getvalue()

    display.observe(_tool_call("call"))
    display.observe(_tool_result("call"))
    display.observe(Event(type="model_stream_start", data={"step": 2, "attempt": 1}))
    display.observe(_stream_delta("model_reasoning_delta", "second thought", step=2))
    display.observe(_stream_delta("model_text_delta", "The answer.", step=2))
    display.observe(_stream_end(step=2))
    assert transcript.getvalue().index("Thinking") < transcript.getvalue().index(
        "The answer."
    )

    display.observe(Event(type="agent_finish", data={"step": 2}))
    rendered = transcript.getvalue()
    assert rendered.index("Thinking") < rendered.index("The answer.")
    assert rendered.count("Thinking") == 1
    assert "5.0s" in rendered


def test_the_preview_never_reaches_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reasoning belongs to the trace; only the receipt survives a turn."""
    transcript = _printed(monkeypatch)
    display = _reasoning_display()
    display.observe(_stream_delta("model_text_delta", "It splits tokens."))
    display.observe(_stream_end())

    assert "Reading the parser..." not in transcript.getvalue()


# -- concurrent tool batches ----------------------------------------------
#
# A batch runs several calls at once. Reporting idle or thinking while three
# calls are still executing would describe work that has not started.


def _tool_call(call_id: str, tool: str = "read_file", path: str = "a.py") -> Event:
    return Event(
        type="tool_call",
        data={"tool_call_id": call_id, "tool": tool, "arguments": {"path": path}},
    )


def _tool_result(call_id: str) -> Event:
    return Event(
        type="tool_result",
        data={
            "tool_call_id": call_id,
            "tool": "read_file",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        },
    )


def test_a_batch_keeps_reporting_work_until_it_settles() -> None:
    """One finished call among three must not report the turn as thinking."""
    display = _display()
    for call_id in ("one", "two", "three"):
        display.observe(_tool_call(call_id, path=f"{call_id}.py"))

    display.observe(_tool_result("one"))

    assert display._activity.kind is ActivityKind.WORKING
    assert display._activity.running == 2


def test_the_surface_says_how_many_calls_share_the_work() -> None:
    display = _display()
    display.observe(_tool_call("one"))
    display.observe(_tool_call("two"))

    assert "concurrent" in _surface(display)


def test_a_settled_batch_leaves_the_surface() -> None:
    display = _display()
    display.observe(_tool_call("one"))
    display.observe(_tool_result("one"))

    assert "read_file" not in _surface(display)
    assert display._activity.calls == 1


def test_an_interrupt_wins_over_a_running_tool() -> None:
    """While stopping, the surface says so; it does not keep naming the tool."""
    display = _display()
    display.observe(_tool_call("one"))

    display.begin_interrupt()

    assert "Interrupt" in _surface(display)


def test_a_tool_finishing_does_not_hide_the_interrupt() -> None:
    display = _display()
    display.observe(_tool_call("one"))
    display.begin_interrupt()

    display.observe(_tool_result("one"))

    assert "Interrupt" in _surface(display)


# -- failures stay observable --------------------------------------------
#
# Hiding routine progress kept the transcript readable. Hiding failures would be a
# different thing: a denied call or a failed command is the one tool event a
# reader has to act on.


def _handler_transcript(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    transcript = io.StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=transcript, color_system=None, force_terminal=False, width=200),
    )
    return transcript


def test_a_denied_call_names_the_command_and_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = _handler_transcript(monkeypatch)
    ui.console_event_handler(
        Event(
            type="tool_call",
            data={
                "tool_call_id": "denied-call",
                "tool": "bash",
                "arguments": {"command": "sudo rm -rf /"},
            },
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_denied",
            data={
                "tool_call_id": "denied-call",
                "tool": "bash",
                "error": "sudo is not supported",
                "error_type": "PolicyDenied",
            },
        )
    )

    rendered = transcript.getvalue()
    assert "bash: sudo rm -rf /" in rendered
    assert "sudo is not supported" in rendered


def test_a_tool_error_is_reported_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rich markup in a tool error must reach the reader as text, not as styling."""
    transcript = _handler_transcript(monkeypatch)
    ui.console_event_handler(
        Event(
            type="tool_call",
            data={
                "tool_call_id": "edit-call",
                "tool": "edit_file",
                "arguments": {"path": "notes.md"},
            },
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_error",
            data={
                "tool_call_id": "edit-call",
                "tool": "edit_file",
                "error": "[/bold red]",
            },
        )
    )

    rendered = transcript.getvalue()
    assert "edit_file notes.md" in rendered
    assert "[/bold red]" in rendered


def test_a_failing_command_reports_its_exit_code_and_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = _handler_transcript(monkeypatch)
    ui.console_event_handler(
        Event(
            type="tool_call",
            data={
                "tool_call_id": "bash-1",
                "tool": "bash",
                "arguments": {"command": "uv run pytest -q"},
            },
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool_call_id": "bash-1",
                "tool": "bash",
                "exit_code": 1,
                "stdout": "stdout fallback diagnostic",
                "stderr": "pytest collection failed",
            },
        )
    )

    rendered = transcript.getvalue()
    assert "uv run pytest -q" in rendered
    assert "(exit 1)" in rendered
    assert "pytest collection failed" in rendered
    # stderr wins over stdout, so a stdout fallback never passes as the cause.
    assert "stdout fallback diagnostic" not in rendered


def test_a_successful_call_prints_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routine success is progress, not a fact worth a transcript line."""
    transcript = _handler_transcript(monkeypatch)
    ui.console_event_handler(
        Event(
            type="tool_call",
            data={
                "tool_call_id": "bash-ok",
                "tool": "bash",
                "arguments": {"command": "ls"},
            },
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool_call_id": "bash-ok",
                "tool": "bash",
                "exit_code": 0,
                "stdout": "HUGE-SUCCESSFUL-OUTPUT",
                "stderr": "",
            },
        )
    )

    assert transcript.getvalue().strip() == ""


def test_a_failure_diagnostic_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing command can produce megabytes; the screen keeps what is actionable."""
    transcript = _handler_transcript(monkeypatch)
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool_call_id": "bash-2",
                "tool": "bash",
                "exit_code": 1,
                "stdout": "",
                "stderr": "\n".join(f"line {index}" for index in range(200)),
            },
        )
    )

    rendered = transcript.getvalue()
    assert "more lines (see /trace)" in rendered
    assert rendered.count("\n") < 40


# -- repeated cancellation ----------------------------------------------
#
# Ctrl-C is easy to press more than once. The second press must not re-request a
# cancellation already in flight, and it must not print a second receipt: the
# operator asked for one outcome, not for a count of their keypresses.


def test_repeated_interrupts_leave_one_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = io.StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=transcript, color_system=None, force_terminal=False, width=80),
    )
    display = StreamingDisplay()
    display.observe(Event(type="model_stream_start", data={"step": 1}))
    display.observe(_stream_delta("model_text_delta", "partial answer"))

    # Three presses while the cancellation is in flight.
    for _ in range(3):
        display.begin_interrupt()
    assert display.interrupting
    display.settle_interrupt()

    rendered = transcript.getvalue()
    assert rendered.count("Interrupted") == 1
    assert "partial answer" not in rendered


def test_a_second_press_reports_no_new_request() -> None:
    """The surface asks for one cancellation, however many times it is pressed."""
    display = StreamingDisplay()
    display.observe(Event(type="model_stream_start", data={"step": 1}))

    assert display.begin_interrupt() is True
    assert display.begin_interrupt() is False


def test_settling_twice_leaves_one_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled turn can unwind more than once; the transcript says so once."""
    transcript = io.StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=transcript, color_system=None, force_terminal=False, width=80),
    )
    display = StreamingDisplay()
    display.begin_interrupt()

    display.settle_interrupt()
    display.settle_interrupt()

    assert transcript.getvalue().count("Interrupted") == 1


def test_a_normal_turn_leaves_no_interrupt_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript = io.StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=transcript, color_system=None, force_terminal=False, width=80),
    )
    display = StreamingDisplay()
    display.observe(Event(type="model_stream_start", data={"step": 1}))
    display.observe(_stream_delta("model_text_delta", "a complete answer"))
    display.observe(_stream_end())

    display.settle_interrupt()

    assert "Interrupted" not in transcript.getvalue()
    assert "a complete answer" in transcript.getvalue()


def test_the_interrupted_activity_clears_once_settled() -> None:
    """After the turn stops, the surface stops saying it is stopping."""
    display = _display()
    display.begin_interrupt()
    assert "Interrupt" in _surface(display)

    display.settle_interrupt()

    assert "Interrupt" not in _surface(display)
    assert not display.interrupting


def test_an_interrupted_stream_does_not_add_a_second_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt already explains itself; the generic warning would repeat it.

    The stream really does end unfinished when a turn is cancelled, so the display
    sees a non-completed status. Reporting "incomplete output discarded" there put a
    second line on screen saying what "Interrupted" had just said.
    """
    transcript = io.StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=transcript, color_system=None, force_terminal=False, width=80),
    )
    display = StreamingDisplay()
    display.observe(Event(type="model_stream_start", data={"step": 1}))
    display.observe(_stream_delta("model_text_delta", "half an ans"))
    display.begin_interrupt()

    # The runtime reports the cancellation as a stream that did not complete.
    display.observe(Event(type="model_stream_end", data={"status": "incomplete"}))
    display.settle_interrupt()

    rendered = transcript.getvalue()
    assert "Interrupted" in rendered
    assert "incomplete output discarded" not in rendered
    assert "half an ans" not in rendered


def test_an_uninterrupted_failure_still_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suppressing the warning for interrupts must not suppress it for failures."""
    transcript = io.StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=transcript, color_system=None, force_terminal=False, width=80),
    )
    display = StreamingDisplay()
    display.observe(Event(type="model_stream_start", data={"step": 1}))
    display.observe(_stream_delta("model_text_delta", "half an ans"))

    display.observe(Event(type="model_stream_end", data={"status": "incomplete"}))

    rendered = transcript.getvalue()
    assert "incomplete output discarded" in rendered
    assert "half an ans" not in rendered
