"""The pinned input line, exercised without a real terminal.

prompt_toolkit can be driven headless with a pipe input and a dummy output, so the
frame's contract — what it writes where, when it repaints, and what it hands back —
is testable without a TTY. The alternative would be asserting on escape sequences,
which would pin the rendering library rather than the behaviour.

The architectural choice these tests protect: the application does **not** own the
screen. Finished output goes to the terminal's scrollback, so native search,
selection and copy keep working, and only the bottom dock repaints.
"""

import asyncio
import re
from contextlib import suppress

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.output import DummyOutput
from rich.text import Text

import cairn.terminal.output as output_module
from cairn.core.events import Event
from cairn.terminal.action import Action, ActionRow, correction, note
from cairn.terminal.frame import (
    ACTIVITY_ROWS,
    COMPOSER_MAX_LINES,
    FRAME_STYLE,
    STATUS_LINES,
    FrameLiveRegion,
    TranscriptView,
    frame_live_region,
    gap_lines,
)
from cairn.terminal.output import StreamingDisplay
from cairn.terminal.pacing import StreamPacer
from cairn.terminal.transcript import TRANSCRIPT_GAP_LINES, Transcript


class _Clock:
    """A clock the test advances by hand, so pacing is exact."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Sink:
    """Collects what the view wrote to the terminal."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str) -> None:
        self.lines.append(line)


def _view(
    *,
    transcript: Transcript | None = None,
    interval: float = 0.001,
    sink: _Sink | None = None,
) -> tuple[TranscriptView, _Clock, _Sink]:
    clock = _Clock()
    out = sink if sink is not None else _Sink()
    view = TranscriptView(
        transcript=transcript if transcript is not None else Transcript(),
        pacer=StreamPacer(interval=interval),
        clock=clock,
        write=out,
    )
    return view, clock, out


def test_the_frame_builds_headless() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        assert view.has_pending_input is False


def test_the_application_does_not_own_the_screen() -> None:
    """Owning the screen would cost the terminal's scrollback, search and copy."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        assert view._app is not None
        assert view._app.full_screen is False


def test_the_composer_is_capped_at_four_lines() -> None:
    """Beyond the cap the buffer scrolls internally, so the transcript never jumps."""
    assert COMPOSER_MAX_LINES == 4


def test_the_activity_slot_is_bounded() -> None:
    """A weighted pane would claim every spare row and hide the input line."""
    assert ACTIVITY_ROWS > 0
    assert ACTIVITY_ROWS < COMPOSER_MAX_LINES


def test_the_status_is_exactly_two_rows() -> None:
    """Two rows, always: one identity, one numbers, and nothing beneath them."""
    assert STATUS_LINES == 2


# -- the dock's geometry --------------------------------------------------
#
# The order is the design: activity, then the composer between two rules, then the
# status. A palette in the middle of that would look like the input had moved, and a
# third row under the status would be a permanent hint bar.


def _dock_rows(view: TranscriptView) -> list[str]:
    """The dock's windows from top to bottom, named by the style class they use.

    Reading the layout rather than rendering it keeps this a test of *order*: the
    exact rows are prompt_toolkit's business, the arrangement is ours.
    """
    assert view._app is not None
    container = view._app.layout.container
    rows: list[str] = []
    for child in container.get_children():
        style = getattr(child, "style", "")
        if "horizontal-line" in style:
            # A rule carries prompt_toolkit's own style class, not one of ours.
            rows.append("rule")
        elif style.startswith("class:"):
            rows.append(style.removeprefix("class:"))
        else:
            rows.append("-")
    return rows


def test_the_status_is_the_last_thing_on_the_screen() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        rows = _dock_rows(view)

    assert rows[-1] == "status"


def test_the_composer_sits_between_two_rules() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        rows = _dock_rows(view)

    above = rows[rows.index("input") - 1]
    below = rows[rows.index("input") + 1]
    assert above == "rule"
    assert below == "rule"


def test_the_palette_is_not_between_the_composer_and_its_text() -> None:
    """A menu inside the composer's rules reads as the input having moved."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        rows = _dock_rows(view)

    assert rows.index("palette") < rows.index("input")
    # Nothing but the rules may touch the composer.
    neighbours = {rows[rows.index("input") - 1], rows[rows.index("input") + 1]}
    assert neighbours == {"rule"}


def test_the_activity_comes_before_the_composer() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        rows = _dock_rows(view)

    assert rows.index("live") < rows.index("input")


def test_nothing_follows_the_status_rows() -> None:
    """A permanent hint row under the status is the defect this replaces."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        rows = _dock_rows(view)

    assert rows[rows.index("status") + 1 :] == []


def test_the_status_shows_exactly_the_published_rows() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.observe_status("model  x\nusage  1 in / 2 out")

        rendered = view._status_control()

    assert "model  x" in str(rendered)
    assert "usage  1 in / 2 out" in str(rendered)


def test_the_status_control_preserves_semantic_styling_fragments() -> None:
    from cairn.terminal.output import StreamingDisplay

    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        display = StreamingDisplay()
        display.observe_model("openai/deepseek-v4.1-flash-1017", provider="bailian")
        display.observe_step_usage(input_tokens=183323, output_tokens=3892)
        display._frozen_rate = 54.0
        display.observe_context(used=17, limit=100)
        view.observe_status(display.status_rendered_text(width=80))

        rendered = view._status_control().value

    assert "\x1b[1;36m" in rendered
    assert "\x1b[2m" in rendered
    assert "183.3k" in rendered and "3.9k" in rendered
    assert "54 tok/s" in rendered and "17%" in rendered


def test_the_status_never_grows_a_third_row() -> None:
    """Anything the display sends past two rows is dropped, not appended."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.observe_status("one\ntwo\nthree")

        assert len(view._status) == STATUS_LINES


def test_the_key_hint_is_not_a_status_row() -> None:
    """The steering hint belongs to the queue stack, not to the footer."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.observe_status("model  x\nusage  ready")
        view.set_running(True)

        assert "guide" not in str(view._status_control())


def test_the_gap_between_output_and_input_is_one_row() -> None:
    assert gap_lines() == 1
    assert TRANSCRIPT_GAP_LINES == 1


def test_the_live_region_coalesces_tokens_and_flushes_its_latest_frame() -> None:
    view, clock, _ = _view(interval=0.1)
    paints: list[str] = []
    view.repaint = lambda: paints.append(view.live_text)  # type: ignore[method-assign]
    region = FrameLiveRegion(view)
    region.start(Text("first"))

    clock.advance(0.01)
    region.update(Text("superseded"))
    region.update(Text("latest pending"))
    assert paints == ["first"]

    clock.advance(0.1)
    region.update(Text("admitted"))
    assert paints[-1] == "admitted"
    assert len(paints) == 2

    region.update(Text("status boundary"))
    view.observe_status("model x\nusage ready")
    assert paints[-1] == "status boundary"

    view._available_width = lambda: 80  # type: ignore[method-assign]
    view.on_width = lambda _width: None
    region.update(Text("resize boundary"))
    view._report_width()
    assert paints[-1] == "resize boundary"

    region.update(Text("settled value"))
    region.stop()
    assert "settled value" in paints
    assert paints[-1] == ""
    assert len(paints) == 6


def test_a_long_pending_renderable_keeps_its_complete_source_text() -> None:
    view, _, _ = _view()
    region = FrameLiveRegion(view)
    content = "\n".join(f"line {index}" for index in range(12))
    region.start(Text(content))

    assert "line 0" in view.live_text
    assert "line 11" in view.live_text


def test_the_frame_uses_no_background_colour() -> None:
    """A filled background is unreadable over a remote terminal palette."""
    for rules in FRAME_STYLE.style_rules:
        _, style = rules
        assert "bg:" not in style, f"background set in {style!r}"
        assert "reverse" not in style


def test_finished_output_goes_to_the_scrollback() -> None:
    """Finished output is ordinary terminal output, not a repaint."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, sink = _view()
        view.emit("first line\nsecond line")
        assert sink.lines == ["first line", "second line"]
        assert view.transcript.window(5) == ["first line", "second line"]


def test_streamed_text_is_paced_by_the_frame_budget() -> None:
    """Five deltas inside one interval must not become five scrollback writes."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, clock, sink = _view(interval=0.1)
        for chunk in "abcde":
            view.append_streamed(chunk)
            clock.advance(0.001)
        # Nothing reaches the scrollback until the stream settles.
        assert sink.lines == []

        view.settle_streamed()
        assert sink.lines == ["abcde"]


def test_settling_an_empty_stream_writes_nothing() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, sink = _view()
        view.settle_streamed()
        assert sink.lines == []


@pytest.mark.parametrize(
    "expected",
    ["Keys.ControlC", "Keys.ControlD"],
    ids=["interrupt", "end-input"],
)
def test_control_key_is_bound(expected: str) -> None:
    """Ctrl-C interrupts and Ctrl-D ends input; neither may be swallowed."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        assert view._app is not None
        bindings = view._app.key_bindings
        assert bindings is not None
        keys = {
            tuple(str(key) for key in binding.keys) for binding in bindings.bindings
        }
        assert (expected,) in keys


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_input_is_not_submitted(blank: str) -> None:
    """Only a real line becomes a message; blank input keeps the prompt open."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view._buffer.text = blank
        view._buffer.validate_and_handle()
        # The application was never exited, so nothing was submitted.
        assert view._app is not None
        assert not view._app.is_done


# -- width, and a resize ------------------------------------------------
#
# Both the status and the activity surface wrap at the terminal's width. A width
# captured once at startup keeps a 60-column terminal wrapped at 80 and overflows
# the frame, so the width is read on every paint and the display is told when it
# changes.


def test_the_live_region_renders_at_the_frame_width() -> None:
    """A live region that hard-coded its width would overflow after a resize."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        region = FrameLiveRegion(view)
        region.start(Text("x" * 60))

        assert len(view.live_text.splitlines()[0]) <= (view.available_width() or 80)


def test_running_renderer_reclaims_closed_surface_rows_and_tracks_resize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            view, _, _ = _view()
            assert view._app is not None
            current = [Size(rows=60, columns=60)]
            view._app.output.get_size = lambda: current[0]  # type: ignore[method-assign]
            await view._ensure_running()
            await asyncio.sleep(0.03)

            def screen_rows() -> list[str]:
                screen = view._app.renderer.last_rendered_screen  # type: ignore[union-attr]
                assert screen is not None
                rows = []
                for y in range(screen.height):
                    cells: dict[int, Char] = screen.data_buffer.get(y, {})
                    rows.append("".join(cells[x].char for x in sorted(cells)))
                return rows

            idle_rows = screen_rows()
            composer_rule = next(i for i, row in enumerate(idle_rows) if "─" in row)

            current[0] = Size(rows=24, columns=80)
            view.show_live("\n".join(f"long pending answer row {i}" for i in range(18)))
            view._buffer.text = "/model"
            await asyncio.sleep(0.03)
            short_rows = screen_rows()
            assert len(short_rows) == 24
            assert any("long pending answer row 0" in row for row in short_rows)
            assert any("/model" in row for row in short_rows)
            assert not any("Window too small" in row for row in short_rows)
            view.show_live("")
            view._buffer.text = ""
            current[0] = Size(rows=60, columns=60)
            await asyncio.sleep(0.03)

            view.show_action(
                Action(
                    message="provider chooser facts\n" * 8,
                    rows=(ActionRow(name="bailian", current=True),),
                    hint="Up/Down move   Enter switch   Esc cancel",
                )
            )
            await asyncio.sleep(0.03)
            expanded_rows = screen_rows()
            assert (
                len([row for row in expanded_rows if "provider chooser facts" in row])
                > 1
            )

            view.show_action(None)
            view.show_live("* Thinking  00:03\n  preview one\n  preview two")
            await asyncio.sleep(0.03)
            thinking_rows = screen_rows()
            thinking_rule = next(i for i, row in enumerate(thinking_rows) if "─" in row)
            assert thinking_rule > composer_rule
            live_end = max(
                i for i, row in enumerate(thinking_rows) if "preview two" in row
            )
            assert thinking_rule - live_end == 2
            view.show_live("")
            await asyncio.sleep(0.03)
            closed_rows = screen_rows()
            closed_rule = next(i for i, row in enumerate(closed_rows) if "─" in row)
            assert closed_rule == composer_rule

            monkeypatch.setattr(output_module, "supports_live_display", lambda: True)
            display = StreamingDisplay(live_factory=frame_live_region(view))
            view.on_width = display.set_status_width
            display.observe(Event(type="model_stream_start", data={"step": 1}))
            display.observe(
                Event(
                    type="model_reasoning_delta",
                    data={"text": "\n".join(f"reason row {i}" for i in range(10))},
                )
            )
            await asyncio.sleep(0.04)
            view.flush_live()
            collapsed = screen_rows()
            collapsed_count = sum("reason row" in row for row in collapsed)
            display.reveal_reasoning()
            await asyncio.sleep(0.04)
            view.flush_live()
            expanded = screen_rows()
            assert sum("reason row" in row for row in expanded) > collapsed_count
            display.reveal_reasoning()
            await asyncio.sleep(0.04)
            view.flush_live()
            recollapsed = screen_rows()
            assert sum("reason row" in row for row in recollapsed) == collapsed_count
            display.reset()

            for size in (Size(rows=80, columns=80), Size(rows=120, columns=120)):
                current[0] = size
                view.show_live("pending paragraph row one\nrow two\nrow three")
                view.repaint()
                await asyncio.sleep(0.03)
                resized_rows = screen_rows()
                rule = next(i for i, row in enumerate(resized_rows) if "─" in row)
                assert rule < len(resized_rows) - STATUS_LINES
                live_end = max(
                    i for i, row in enumerate(resized_rows) if "row three" in row
                )
                assert rule - live_end == 2
                view.show_live("")
                await asyncio.sleep(0.03)
            await view.aclose()

    asyncio.run(scenario())


def test_a_width_change_is_reported_once() -> None:
    """Reporting on every paint would re-fit the display on every keystroke."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        seen: list[int] = []
        view.on_width = seen.append

        view._status_control()
        view._status_control()
        first = view.available_width()

        assert seen == [first]


def test_a_resize_reports_the_new_width() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        seen: list[int] = []
        view.on_width = seen.append
        assert view._app is not None

        view._status_control()
        # The terminal reports a different size, as it would after a resize.
        view._app.output.get_size = lambda: Size(rows=24, columns=52)  # type: ignore[method-assign]
        view._status_control()

        assert seen[0] != 52
        assert seen[-1] == 52


# -- what Ctrl-C unwinds ------------------------------------------------
#
# One key has to serve several layers, and it must unwind them in the order the
# operator sees them. Killing a running turn because a menu happened to be open
# would lose work the operator never asked to stop.


def _press(view: TranscriptView, key: str) -> None:
    """Deliver one key through the application's own bindings, as a terminal would."""
    assert view._app is not None
    bindings = view._app.key_bindings
    assert bindings is not None
    for binding in bindings.bindings:
        # prompt_toolkit spells a named key as ``Keys.Escape`` and a literal one as
        # the character itself, so both forms are compared.
        names = {str(k) for k in binding.keys} | {
            getattr(k, "name", "") for k in binding.keys if k is not None
        }
        if key in names and binding.filter():
            # The binding handlers only read these attributes, so a stand-in is
            # enough to drive the real key path without a terminal.
            binding.handler(_FakeEvent(view))  # type: ignore[arg-type]
            return
    raise AssertionError(f"no active binding for {key}")


class _FakeEvent:
    """The part of a key-press event a binding touches in these tests."""

    def __init__(self, view: TranscriptView) -> None:
        self.current_buffer = view._buffer
        self.app = view._app
        self.data = ""


def test_ctrl_c_declines_an_open_chooser_first() -> None:
    """A menu in front of the operator is what Ctrl-C means to close."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        interrupted: list[bool] = []
        view.on_interrupt = lambda: interrupted.append(True)
        view.set_running(True)
        view.show_action(Action(message="choose", rows=(ActionRow(name="flash"),)))

        _press(view, "Keys.ControlC")

        assert view.action is None
        # The turn behind the menu is untouched: nothing was interrupted.
        assert interrupted == []


def test_ctrl_c_closes_the_palette_before_interrupting() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        interrupted: list[bool] = []
        view.on_interrupt = lambda: interrupted.append(True)
        view.set_running(True)
        view._buffer.insert_text("/mo")

        _press(view, "Keys.ControlC")

        assert not view._palette.open
        assert interrupted == []


def test_ctrl_c_clears_the_composer_before_interrupting() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        interrupted: list[bool] = []
        view.on_interrupt = lambda: interrupted.append(True)
        view.set_running(True)
        view._buffer.insert_text("not this message")

        _press(view, "Keys.ControlC")

        assert not view._buffer.text
        assert interrupted == []


def test_ctrl_c_with_nothing_in_front_interrupts_the_turn() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        interrupted: list[bool] = []
        view.on_interrupt = lambda: interrupted.append(True)
        view.set_running(True)

        _press(view, "Keys.ControlC")

        assert interrupted == [True]


def test_ctrl_c_while_idle_does_not_interrupt_anything() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        interrupted: list[bool] = []
        view.on_interrupt = lambda: interrupted.append(True)

        _press(view, "Keys.ControlC")

        assert interrupted == []


def test_escape_declines_a_correction_too() -> None:
    """A correction is dismissible by the same key as a chooser."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.show_action(correction("/session is not an in-session command."))

        _press(view, "Keys.Escape")

        assert view.action is None


def test_typing_clears_a_correction_but_not_a_chooser() -> None:
    """A correction is about a line already replaced; a chooser is still being used."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.show_action(correction("/session is not an in-session command."))
        view._buffer.insert_text("/")
        assert view.action is None

        view.show_action(Action(message="choose", rows=(ActionRow(name="flash"),)))
        view._buffer.insert_text("/m")
        assert view.action is not None


# -- real key dispatch ----------------------------------------------------
#
# The helper above walks the binding table itself, so it can only prove a binding
# exists; it cannot prove which binding prompt_toolkit picks when several could
# match a key. These tests ask prompt_toolkit's own resolver which handler a real
# key press would reach, which is the question that matters: an open chooser has
# to own the arrows, or the cursor never moves.


def _live_handler(view: TranscriptView, key: Keys) -> str:
    """The handler prompt_toolkit would run for ``key``, by its own resolution."""
    assert view._app is not None
    bindings = view._app.key_bindings
    assert bindings is not None
    candidates = [binding for binding in bindings.get_bindings_for_keys((key,))]
    active = [binding for binding in candidates if binding.filter()]
    assert active, f"no active binding for {key}"
    # prompt_toolkit takes the first match in registration order.
    return active[0].handler.__name__


def _chooser(rows: tuple[str, ...], selected: int = 0) -> Action:
    return Action(
        message="choose",
        rows=tuple(ActionRow(name) for name in rows),
        selected=selected,
    )


def _moving(view: TranscriptView) -> list[tuple[int, int]]:
    """Give the view a movement callback that relocates the cursor and records it."""
    seen: list[tuple[int, int]] = []

    def on_move(current: int, step: int) -> Action:
        seen.append((current, step))
        action = view.action
        assert action is not None
        action.selected = max(0, min(len(action.rows) - 1, current + step))
        return action

    view.on_action_move = on_move
    return seen


def test_an_open_chooser_owns_the_arrow_keys() -> None:
    """The arrows belong to the chooser, not to the queue behind it."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.show_action(_chooser(("one", "two", "three")))

        assert _live_handler(view, Keys.Down) == "action_next"
        assert _live_handler(view, Keys.Up) == "action_previous"


def test_the_arrows_return_to_the_queue_when_no_chooser_is_open() -> None:
    """Closing the chooser must hand the arrows back, not leave them dead."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.show_action(_chooser(("one", "two")))
        assert _live_handler(view, Keys.Down) == "action_next"

        view.show_action(None)

        assert _live_handler(view, Keys.Down) == "select_next"
        assert _live_handler(view, Keys.Up) == "select_previous"


def test_the_palette_owns_the_arrows_while_it_is_open() -> None:
    """The palette still wins when the chooser is closed and a command is typed."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view._buffer.insert_text("/mo")

        assert _live_handler(view, Keys.Down) == "palette_next"


def test_enter_confirms_a_row_only_while_a_chooser_is_open() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.show_action(_chooser(("one", "two")))
        assert _live_handler(view, Keys.Enter) == "action_choose"

        view.show_action(None)
        assert _live_handler(view, Keys.Enter) != "action_choose"


def test_escape_closes_a_row_chooser() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.show_action(_chooser(("one", "two")))

        assert _live_handler(view, Keys.Escape) == "action_close"


def test_down_moves_one_row_each_time() -> None:
    """Three Downs reach the fourth row; a rebuild from zero would stop at two."""
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        seen = _moving(view)
        view.show_action(_chooser(("one", "two", "three", "four")))
        handler = _live_handler(view, Keys.Down)

        for _ in range(3):
            _press_by_name(view, handler)

        assert seen == [(0, 1), (1, 1), (2, 1)]
        assert view.action is not None
        row = view.action.selected_row()
        assert row is not None
        assert row.name == "four"


def test_up_moves_back_one_row() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        _moving(view)
        view.show_action(_chooser(("one", "two", "three"), selected=2))

        _press_by_name(view, _live_handler(view, Keys.Up))

        assert view.action is not None
        assert view.action.selected == 1


def test_the_selection_stops_at_either_end() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        _moving(view)
        view.show_action(_chooser(("one", "two")))
        down = _live_handler(view, Keys.Down)
        up = _live_handler(view, Keys.Up)

        _press_by_name(view, up)  # at the top
        assert view.action is not None and view.action.selected == 0
        for _ in range(4):
            _press_by_name(view, down)  # past the bottom
        assert view.action is not None and view.action.selected == 1


def test_enter_confirms_the_row_the_cursor_is_on() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        chosen: list[str] = []
        _moving(view)
        view.on_action_choose = chosen.append
        view.show_action(_chooser(("one", "two", "three")))
        down = _live_handler(view, Keys.Down)
        enter = _live_handler(view, Keys.Enter)

        _press_by_name(view, down)
        _press_by_name(view, down)
        _press_by_name(view, enter)

    assert chosen == ["three"]
    assert view.action is None


def test_pipe_dispatch_moves_and_confirms_the_visible_action_row() -> None:
    """Resolve keys through prompt_toolkit with Down/Up and a four-row chooser."""

    async def scenario() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            view, _, _ = _view()
            chosen: list[str] = []
            view.show_action(_chooser(("one", "two", "three", "four")))
            app = view._app
            assert app is not None

            def choose(name: str) -> None:
                chosen.append(name)
                app.exit(result=name)

            view.on_action_choose = choose
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0)
            pipe.send_text("\x1b[B\x1b[B\x1b[A\x1b[B\x1b[B\x1b[B\r")
            await asyncio.wait_for(task, timeout=1)

            assert chosen == ["four"]
            assert view.action is None

    asyncio.run(scenario())


def test_pipe_ctrl_c_dismisses_the_action_owner() -> None:
    async def scenario() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            view, _, _ = _view()
            dismissed: list[str] = []
            view.on_action_command = dismissed.append
            view.show_action(_chooser(("allow", "Cancel"), selected=1))
            assert view._app is not None
            task = asyncio.create_task(view._app.run_async())
            await asyncio.sleep(0)
            pipe.send_text("\x03")
            for _ in range(100):
                if dismissed:
                    break
                await asyncio.sleep(0.01)
            view._app.exit()
            await asyncio.wait_for(task, timeout=1)

            assert dismissed == [""]
            assert view.action is None

    asyncio.run(scenario())


def test_pipe_enter_descends_the_selected_palette_command() -> None:
    async def scenario() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            view, _, _ = _view()
            app = view._app
            assert app is not None
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0)
            pipe.send_text("/\r")
            for _ in range(100):
                if view._palette.depth == 2:
                    break
                await asyncio.sleep(0.01)
            app.exit()
            await asyncio.wait_for(task, timeout=1)

            assert view._palette.depth == 2

    asyncio.run(scenario())


def test_pipe_enter_runs_the_chosen_palette_command_once() -> None:
    """A single Enter on a complete command submits it; it is not just completed.

    Typing ``/exit`` and pressing Enter once must put the command on the queue the
    session reads from. Accepting the completion as the outcome made the first Enter
    land nowhere: the composer held ``/exit`` and the command had to be typed a
    second time.
    """

    async def scenario() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            view, _, _ = _view()
            reader = asyncio.create_task(view.read())
            await asyncio.sleep(0)

            pipe.send_text("/exit\r")
            line = await asyncio.wait_for(reader, timeout=1)

            assert line.text == "/exit"
            assert line.delivery == "now"
            # The composer and the palette are left as an idle session expects.
            assert view._buffer.text == ""
            assert not view._palette.open
            await view.aclose()

    asyncio.run(scenario())


def test_pipe_enter_submits_a_command_the_list_does_not_carry() -> None:
    """A typed command with no palette row is still submitted by one Enter.

    The palette filters the catalogue, not what the operator may run: a command it
    has no row for reaches the router exactly as typed.
    """

    async def scenario() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            view, _, _ = _view()
            reader = asyncio.create_task(view.read())
            await asyncio.sleep(0)

            pipe.send_text("/nosuchcommand\r")
            line = await asyncio.wait_for(reader, timeout=1)

            assert line.text == "/nosuchcommand"
            assert line.delivery == "now"
            await view.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("/\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B\r", "/exit"),
        ("/ex\r", "/exit"),
        ("/model\r\x1b[B\r", "/model list"),
        ("/provider\r\x1b[B\r", "/provider list"),
        ("/trace del --tail 2\r", "/trace del --tail 2"),
        ("/nosuchcommand\r", "/nosuchcommand"),
    ],
)
def test_pipe_enter_submits_the_selected_command_or_typed_line(
    keys: str, expected: str
) -> None:
    """Enter submits menu choices and preserves complete typed command lines."""

    async def scenario() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            view, _, _ = _view()
            reader = asyncio.create_task(view.read())
            await asyncio.sleep(0)

            pipe.send_text(keys)
            line = await asyncio.wait_for(reader, timeout=1)

            assert line.text == expected
            assert line.delivery == "now"
            assert view._buffer.text == ""
            assert not view._palette.open
            await view.aclose()

    asyncio.run(scenario())


def test_pipe_right_and_tab_complete_without_running() -> None:
    """Right and Tab navigate: they put the command in the composer and stop.

    Only Enter acts. If completing a command also submitted it, picking a level with
    Right would run ``/model`` before the operator had chosen a group.
    """

    async def scenario() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            view, _, _ = _view()
            app = view._app
            assert app is not None
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0)
            submitted = asyncio.create_task(view._lines.next_line())

            # /exit is the last root command; Tab has to leave it in the composer
            # rather than run it.
            pipe.send_text("/\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B")
            for _ in range(100):
                if view._palette.command_line() == "/exit":
                    break
                await asyncio.sleep(0.01)
            pipe.send_text("\t")
            for _ in range(100):
                if not view._palette.open:
                    break
                await asyncio.sleep(0.01)

            assert not submitted.done()
            assert view._buffer.text == "/exit"
            assert not view._palette.open
            # Closing the session releases the waiting read; it never delivers the
            # completed line, which is what "Tab did not run it" means.
            with suppress(EOFError):
                await view.aclose()
            with pytest.raises(EOFError):
                await asyncio.wait_for(submitted, timeout=1)
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), timeout=1
            )

    asyncio.run(scenario())


def test_pipe_right_opens_the_selected_palette_level() -> None:
    """Right navigates into a level without running the command."""

    async def scenario() -> None:
        with (
            create_pipe_input() as pipe,
            create_app_session(input=pipe, output=DummyOutput()),
        ):
            view, _, _ = _view()
            app = view._app
            assert app is not None
            task = asyncio.create_task(app.run_async())
            await asyncio.sleep(0)
            submitted = asyncio.create_task(view._lines.next_line())
            pipe.send_text("/\x1b[C")
            for _ in range(100):
                if view._palette.depth == 2:
                    break
                await asyncio.sleep(0.01)
            app.exit()
            await asyncio.wait_for(task, timeout=1)

            assert view._palette.depth == 2
            assert not submitted.done()
            await view.aclose()

    asyncio.run(scenario())


def test_escape_dismisses_the_action_owner() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        dismissed: list[str] = []
        view.on_action_command = dismissed.append
        view.show_action(_chooser(("allow", "Cancel"), selected=1))

        _press(view, "Keys.Escape")

        assert dismissed == [""]
        assert view.action is None


def _press_by_name(view: TranscriptView, handler_name: str) -> None:
    """Run the handler prompt_toolkit resolved for a key."""
    assert view._app is not None
    bindings = view._app.key_bindings
    assert bindings is not None
    for binding in bindings.bindings:
        if binding.handler.__name__ == handler_name:
            binding.handler(_FakeEvent(view))  # type: ignore[arg-type]
            return
    raise AssertionError(f"no binding named {handler_name}")


# -- long facts and long lists -------------------------------------------
#
# The action surface can carry an endpoint that must be readable in full, and a list
# taller than its window. Clipping either one would hide the fact the operator is
# deciding on, or confirm a row that is off screen.


def _fix_width(view: TranscriptView, width: int) -> None:
    """Pin the frame's width, as a terminal of that size would report it."""
    assert view._app is not None
    view._app.output.get_size = lambda: Size(rows=24, columns=width)  # type: ignore[method-assign]


def test_a_long_endpoint_is_wrapped_not_clipped() -> None:
    from cairn.terminal.action import consent

    url = "https://llm.example.test/a-very-long-compatible-mode-path/v1"
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        _fix_width(view, 40)
        view.show_action(consent(f"Send to:\n  {url}", grant="Trust"))

        rendered = view._action_control()

    text = rendered.value
    # The URL is indented by two columns inside the message, so its pieces are cut
    # two cells earlier than the raw URL. What matters is that no characters were
    # dropped: reassembling the visible text must contain the whole endpoint.
    visible = "".join(
        re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in text.split("\n")
    )
    assert url.replace("/", "") in visible.replace("/", "")


@pytest.mark.parametrize("width", [60, 80, 120])
def test_long_decision_facts_page_without_hiding_the_cancel_row(width: int) -> None:
    from cairn.terminal.action import consent

    fact = "https://llm.example.test/" + "private-endpoint-path/" * 100
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        _fix_width(view, width)
        view.show_action(
            consent(
                "Send this session to:\n" + fact,
                grant="Trust for this session",
            )
        )

        first_page = view._action_control().value
        assert "Cancel" in first_page
        assert "PgDn" in first_page
        view._scroll_action_facts(1)
        later_page = view._action_control().value

    assert "private-endpoint-path" in later_page
    assert "Cancel" in later_page
    assert "Trust for this session" in later_page


@pytest.mark.parametrize("width", [60, 80, 120])
def test_fact_paging_exposes_every_line_and_keeps_choices_visible(width: int) -> None:
    from cairn.terminal.action import consent

    facts = [f"endpoint-fact-{index:03d}" for index in range(35)]
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        _fix_width(view, width)
        view.show_action(consent("\n".join(facts), grant="Trust for this session"))

        pages = []
        for _ in range(10):
            rendered = view._action_control().value
            pages.append(rendered)
            if "PgDn" not in rendered:
                break
            view._scroll_action_facts(1)

    for fact in facts:
        assert any(fact in page for page in pages), fact
    assert all("Cancel" in page for page in pages)
    assert all("Trust for this session" in page for page in pages)


def _pieces_of(text: str, width: int) -> list[str]:
    from prompt_toolkit.utils import get_cwidth

    pieces: list[str] = []
    current = ""
    used = 0
    for char in text:
        cells = get_cwidth(char)
        if used + cells > width and current:
            pieces.append(current)
            current = ""
            used = 0
        current += char
        used += cells
    pieces.append(current)
    return pieces


def test_no_action_line_exceeds_the_width_in_cells() -> None:
    from prompt_toolkit.utils import get_cwidth

    from cairn.terminal.action import consent

    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        _fix_width(view, 30)
        view.show_action(
            consent(
                "Send this session's conversation to bailian at "
                "https://llm.example.test/compatible-mode/v1",
                grant="Trust for this session",
            )
        )

        lines = view._action_control().value.split("\n")

    # Strip the ANSI styling the control emits.
    import re

    plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]
    for line in plain:
        assert sum(get_cwidth(char) for char in line) <= 30, line


def test_a_long_list_scrolls_to_keep_the_cursor_visible() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        _fix_width(view, 60)
        rows = tuple(ActionRow(f"model-{index}") for index in range(20))
        view.show_action(Action(message="choose", rows=rows, selected=19))

        rendered = view._action_control().value

    assert "> model-19" in rendered
    assert "model-0" not in rendered


def test_a_short_list_shows_from_the_top() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        _fix_width(view, 60)
        view.show_action(_chooser(("one", "two")))

        rendered = view._action_control().value

    assert "> one" in rendered
    assert "two" in rendered


def test_palette_selection_inserts_editable_token_and_does_not_submit() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.on_palette_data = lambda: (
            (("/skill", "Select a skill"),),
            {"/skill": (("local", "Local helper"),)},
        )
        view._buffer.insert_text("/skill")
        assert view._palette.open
        assert view._palette.insertion() == "[skill:local]"

        _press_by_name(view, "palette_run")  # Enter on the palette row
        assert view._buffer.text == "[skill:local] "
        assert not view._palette.open

        view._buffer.insert_text("fix the bug")  # type: ignore[unreachable]
        assert view._buffer.text == "[skill:local] fix the bug"


def test_two_palette_selections_can_be_inserted_before_sending() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.on_palette_data = lambda: (
            (("/skill", "Select a skill"),),
            {"/skill": (("a", "First"), ("b", "Second"))},
        )
        view._buffer.insert_text("/skill")
        _press_by_name(view, "palette_run")
        assert view._buffer.text == "[skill:a] "

        # A second trailing /skill opens the same level; the earlier token stays.
        view._buffer.insert_text("/skill")
        assert view._palette.open
        _press(view, "Down")
        _press_by_name(view, "palette_run")
        assert view._buffer.text == "[skill:a] [skill:b] "


def test_palette_insertion_keeps_text_after_cursor() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.on_palette_data = lambda: (
            (("/skill", "Select a skill"),),
            {"/skill": (("local", "Local helper"),)},
        )
        view._buffer.insert_text("finish the task")
        view._buffer.cursor_position = 0
        view._buffer.insert_text("/skill loc")
        _press_by_name(view, "palette_run")
        assert view._buffer.text == "[skill:local] finish the task"
        assert len(view._lines) == 0


def test_running_extension_selection_uses_one_deletable_queue() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.set_running(True)
        view._buffer.insert_text("[skill:local] run later")
        view._on_accept(view._buffer)
        assert not view.steering.has_pending_steers
        assert [m.text for m in view.steering.queued()] == ["[skill:local] run later"]
        assert len(view._lines) == 0
        view.show_action(None)
        _press_by_name(view, "promote_selected")
        assert not view.steering.has_pending_steers
        view.show_action(None)
        _press_by_name(view, "discard_selected")
        view.set_running(False)
        assert len(view._lines) == 0


def test_extension_queue_is_released_once_when_turn_finishes() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.set_running(True)
        view._buffer.insert_text("[mcp:local] run later")
        view._on_accept(view._buffer)
        view.set_running(False)
        line = asyncio.run(view._lines.next_line())
        assert line.text == "[mcp:local] run later"
        view.set_running(False)
        assert not view.steering.has_pending
        assert len(view._lines) == 0


def test_info_note_clears_on_edit_so_next_enter_can_submit() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.show_action(note("微信登录: 已登录"))
        assert view.action is not None
        view._buffer.insert_text("/wx status")
        # Editing the composer dismisses the transient note; the next Enter is a
        # normal submission rather than the action surface's Enter handler.
        assert view.action is None


def test_wx_panel_is_temporary_and_clearable() -> None:
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        view, _, _ = _view()
        view.show_wx_panel(["QR-1", "QR-2"])
        assert view._wx_panel == ("QR-1", "QR-2")
        assert view._wx_panel_control() == "QR-1\nQR-2"
        view.clear_wx_panel()
        assert view._wx_panel is None
