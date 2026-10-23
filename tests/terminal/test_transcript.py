"""The transcript window: what a reader can see, and where they are looking.

Pure arithmetic over text, so it is tested directly rather than through a
terminal. The cases that matter are the ones a reader would notice if they were
wrong: a resize, a scroll away from the bottom, and output arriving while they are
reading earlier lines. Stream pacing is tested in ``test_pacing.py``.
"""

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
