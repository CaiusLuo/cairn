"""The pinned input line, exercised without a real terminal.

prompt_toolkit can be driven headless with a pipe input and a dummy output, so the
frame's contract — what it writes where, when it repaints, and what it hands back —
is testable without a TTY. The alternative would be asserting on escape sequences,
which would pin the rendering library rather than the behaviour.

The architectural choice these tests protect: the application does **not** own the
screen. Finished output goes to the terminal's scrollback, so native search,
selection and copy keep working, and only the input line repaints.
"""

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cairn.terminal.frame import FRAME_STYLE, INPUT_PROMPT, TranscriptView, gap_lines
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
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        view, _, _ = _view()
        assert view.has_pending_input is False


def test_the_application_does_not_own_the_screen() -> None:
    """Owning the screen would cost the terminal's scrollback, search and copy."""
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        view, _, _ = _view()
        assert view._app is not None
        assert view._app.full_screen is False


def test_the_header_prompt_is_the_documented_one() -> None:
    assert INPUT_PROMPT == "cairn> "


def test_the_gap_between_output_and_input_is_two_rows() -> None:
    assert gap_lines() == 2
    assert TRANSCRIPT_GAP_LINES == 2


def test_the_frame_uses_no_background_colour() -> None:
    """A filled background is unreadable over a remote terminal palette."""
    for rules in FRAME_STYLE.style_rules:
        _, style = rules
        assert "bg:" not in style, f"background set in {style!r}"
        assert "reverse" not in style


def test_finished_output_goes_to_the_scrollback() -> None:
    """Finished output is ordinary terminal output, not a repaint."""
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        view, _, sink = _view()
        view.emit("first line\nsecond line")
        assert sink.lines == ["first line", "second line"]
        assert view.transcript.window(5) == ["first line", "second line"]


def test_streamed_text_is_paced_by_the_frame_budget() -> None:
    """Five deltas inside one interval must not become five scrollback writes."""
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        view, clock, sink = _view(interval=0.1)
        for chunk in "abcde":
            view.append_streamed(chunk)
            clock.advance(0.001)
        # Nothing reaches the scrollback until the stream settles.
        assert sink.lines == []

        view.settle_streamed()
        assert sink.lines == ["abcde"]


def test_settling_an_empty_stream_writes_nothing() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        view, _, sink = _view()
        view.settle_streamed()
        assert sink.lines == []


def test_ctrl_c_is_bound_so_a_turn_can_be_interrupted() -> None:
    """prompt_toolkit swallows SIGINT by default; an unbound Ctrl-C does nothing."""
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        view, _, _ = _view()
        assert view._app is not None
        bindings = view._app.key_bindings
        assert bindings is not None
        keys = {tuple(str(key) for key in binding.keys) for binding in bindings.bindings}
        assert ("Keys.ControlC",) in keys


def test_ctrl_d_is_bound_to_end_input() -> None:
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        view, _, _ = _view()
        assert view._app is not None
        bindings = view._app.key_bindings
        assert bindings is not None
        keys = {tuple(str(key) for key in binding.keys) for binding in bindings.bindings}
        assert ("Keys.ControlD",) in keys


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_input_is_not_submitted(blank: str) -> None:
    """Only a real line becomes a message; blank input keeps the prompt open."""
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        view, _, _ = _view()
        view._buffer.text = blank
        view._buffer.validate_and_handle()
        # The application was never exited, so nothing was submitted.
        assert view._app is not None
        assert not view._app.is_done
