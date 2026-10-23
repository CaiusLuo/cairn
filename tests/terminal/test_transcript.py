"""The transcript window and the stream pacing that drives its repaints.

Both are pure arithmetic over text and time, so they are tested directly rather
than through a terminal. The cases that matter are the ones a reader would notice
if they were wrong: a resize, a scroll away from the bottom, and a stream that
arrives faster than a person can read.
"""

from cairn.terminal.pacing import DEFAULT_FRAME_INTERVAL_SECONDS, StreamPacer
from cairn.terminal.transcript import TRANSCRIPT_GAP_LINES, Transcript


def test_an_empty_transcript_shows_nothing() -> None:
    assert Transcript().window(10) == []


def test_appended_text_becomes_lines() -> None:
    transcript = Transcript()
    transcript.append("one\ntwo\n")
    assert transcript.window(10) == ["one", "two"]
    assert transcript.line_count == 2


def test_a_trailing_newline_does_not_grow_a_blank_line() -> None:
    transcript = Transcript()
    transcript.append("only\n")
    assert transcript.window(5) == ["only"]


def test_the_window_shows_the_newest_lines_by_default() -> None:
    transcript = Transcript()
    transcript.append("\n".join(str(index) for index in range(10)))
    assert transcript.window(3) == ["7", "8", "9"]
    assert transcript.follow


def test_a_short_transcript_uses_only_its_own_lines() -> None:
    transcript = Transcript()
    transcript.append("a\nb")
    assert transcript.window(10) == ["a", "b"]


def test_a_nonpositive_height_shows_nothing() -> None:
    transcript = Transcript()
    transcript.append("a")
    assert transcript.window(0) == []
    assert transcript.window(-5) == []


def test_scrolling_up_detaches_from_the_bottom() -> None:
    transcript = Transcript()
    transcript.append("\n".join(str(index) for index in range(10)))
    transcript.scroll_up(3)
    assert not transcript.follow
    assert transcript.window(2) == ["5", "6"]


def test_new_output_does_not_yank_a_scrolled_view() -> None:
    """A reader looking at older output must not be moved by a new print."""
    transcript = Transcript()
    transcript.append("\n".join(str(index) for index in range(5)))
    transcript.scroll_up(2)
    before = transcript.window(2)
    transcript.append("new")
    assert transcript.window(2) == before


def test_scrolling_back_to_the_bottom_resumes_following() -> None:
    transcript = Transcript()
    transcript.append("\n".join(str(index) for index in range(5)))
    transcript.scroll_up(2)
    transcript.scroll_down(2)
    assert transcript.follow
    assert transcript.window(2) == ["3", "4"]


def test_scrolling_cannot_go_past_either_end() -> None:
    transcript = Transcript()
    transcript.append("a\nb")
    transcript.scroll_up(100)
    assert transcript.window(5) == ["a", "b"]
    transcript.scroll_down(100)
    assert transcript.follow


def test_scroll_to_bottom_is_immediate() -> None:
    transcript = Transcript()
    transcript.append("\n".join(str(index) for index in range(5)))
    transcript.scroll_up(4)
    transcript.scroll_to_bottom()
    assert transcript.follow


def test_the_gap_is_two_rows() -> None:
    assert TRANSCRIPT_GAP_LINES == 2


# -- pacing ---------------------------------------------------------------


def test_the_first_paint_is_immediate() -> None:
    pacer = StreamPacer()
    assert pacer.admit(0.0)


def test_a_second_delta_inside_the_interval_is_refused() -> None:
    """Faster text must not repaint faster than the frame budget allows."""
    pacer = StreamPacer(interval=0.1)
    assert pacer.admit(0.0)
    assert not pacer.admit(0.01)
    assert not pacer.admit(0.09)
    assert pacer.admit(0.1)


def test_the_default_interval_is_the_documented_frame_budget() -> None:
    assert DEFAULT_FRAME_INTERVAL_SECONDS == 1.0 / 30.0


def test_a_stream_of_many_deltas_costs_bounded_repaints() -> None:
    """One second of very fast deltas must not become a repaint per delta."""
    pacer = StreamPacer(interval=0.1)
    paints = sum(1 for step in range(1000) if pacer.admit(step / 1000))
    assert paints <= 11


def test_reset_makes_the_next_paint_immediate() -> None:
    pacer = StreamPacer(interval=10.0)
    assert pacer.admit(0.0)
    assert not pacer.admit(0.1)
    pacer.reset()
    assert pacer.admit(0.2)


def test_a_nonpositive_interval_is_refused() -> None:
    import pytest

    with pytest.raises(ValueError, match="interval must be positive"):
        StreamPacer(interval=0)
