"""The reasoning preview, and what it is allowed to leave behind.

Reasoning is temporary: it is worth watching while it arrives and worth only a
receipt afterwards. These tests pin the three behaviours that made the transcript
unreadable before — the window showing the wrong end of the text, the finished
block being reprinted into the scrollback, and a key hint that no longer works.
"""

from cairn.terminal.thinking import (
    FRAME_INTERVAL_SECONDS,
    FRAMES,
    PREVIEW_ROWS,
    ThinkingPreview,
)


def _running_preview(*lines: str) -> ThinkingPreview:
    preview = ThinkingPreview()
    preview.begin(at=0.0)
    for line in lines:
        preview.append(line)
    return preview


def test_an_empty_preview_shows_nothing() -> None:
    assert ThinkingPreview().visible() == []


def test_the_preview_shows_the_newest_rows() -> None:
    """A reader looks at what the model is doing now, not where it started."""
    preview = _running_preview("first", "second", "third", "fourth")

    assert list(preview.visible()) == ["third", "fourth"]


def test_the_window_scrolls_rather_than_growing() -> None:
    preview = _running_preview(*[f"line {index}" for index in range(20)])

    assert len(preview.visible()) == PREVIEW_ROWS
    assert list(preview.visible())[-1] == "line 19"


def test_the_preview_is_two_rows() -> None:
    assert PREVIEW_ROWS == 2


def test_a_short_preview_shows_what_it_has() -> None:
    preview = _running_preview("only")
    assert list(preview.visible()) == ["only"]


def test_a_running_preview_animates() -> None:
    preview = _running_preview("thinking")
    assert preview.running
    assert preview.header().startswith(preview.frame)


def test_the_frames_are_single_width_characters() -> None:
    """A wide glyph would shift the gutter on some terminals."""
    assert all(len(frame) == 1 for frame in FRAMES)
    assert all(frame.isascii() for frame in FRAMES)


def test_the_frame_advances_and_wraps() -> None:
    preview = _running_preview("thinking")
    seen = set()
    for _ in range(len(FRAMES) * 2):
        seen.add(preview.frame)
        preview.advance()
    assert seen == set(FRAMES)


def test_the_animation_is_slow_enough_to_read() -> None:
    """Four frames a second is legible; faster reads as flicker."""
    assert FRAME_INTERVAL_SECONDS >= 0.25


def test_a_finished_preview_stops_animating() -> None:
    preview = _running_preview("thinking")
    preview.finish(at=3.2)

    assert not preview.running


def test_a_finished_preview_leaves_a_receipt() -> None:
    """The reader learns that reasoning happened, without paying for its text."""
    preview = _running_preview("thought")
    preview.finish(at=3.2)

    assert preview.receipt() == "Thinking  3.2s"


def test_no_receipt_before_the_turn_settles() -> None:
    preview = _running_preview("thinking")
    assert preview.receipt() == ""


def test_the_elapsed_time_is_measurable() -> None:
    preview = ThinkingPreview()
    preview.begin(at=10.0)
    preview.finish(at=13.5)

    assert preview.elapsed == 3.5


def test_a_long_wait_is_shown_in_minutes() -> None:
    preview = ThinkingPreview()
    preview.begin(at=0.0)
    preview.finish(at=125.0)

    assert preview.receipt() == "Thinking  2m05s"


def test_beginning_again_discards_the_previous_text() -> None:
    """A second attempt must not inherit the first one's reasoning."""
    preview = _running_preview("stale line")
    preview.begin(at=5.0)

    assert preview.visible() == []


def test_clearing_leaves_nothing_behind() -> None:
    preview = _running_preview("some thought")
    preview.finish(at=1.0)
    preview.clear()

    assert preview.visible() == []
    assert preview.receipt() == ""
    assert not preview.running


# -- expansion ------------------------------------------------------------


def test_expanding_a_running_preview_reveals_more_rows() -> None:
    preview = _running_preview(*[f"line {index}" for index in range(30)])
    collapsed = len(preview.visible())

    assert preview.expand()
    assert len(preview.visible()) > collapsed


def test_expanding_twice_reveals_more_again() -> None:
    preview = _running_preview(*[f"line {index}" for index in range(30)])
    preview.expand()
    once = len(preview.visible())

    preview.expand()

    assert len(preview.visible()) > once


def test_expansion_never_reaches_beyond_the_text_received() -> None:
    """The preview is all there is: reasoning is not kept after the turn."""
    preview = _running_preview("one", "two", "three")
    preview.expand()
    preview.expand()

    assert len(preview.visible()) == 3


def test_a_settled_preview_cannot_be_expanded() -> None:
    """Nothing is left to reveal, so the key must not appear to work."""
    preview = _running_preview("thought")
    preview.finish(at=1.0)

    assert not preview.expand()
    assert not preview.running


def test_the_hint_is_absent_once_the_preview_settles() -> None:
    """A key that does nothing is worse than no key."""
    preview = _running_preview("thought")
    preview.finish(at=1.0)

    assert preview.hint() == ""


def test_the_hint_names_the_key_while_running() -> None:
    preview = _running_preview("thought")
    assert "Ctrl+O" in preview.hint()


def test_beginning_again_collapses_the_previous_expansion() -> None:
    preview = _running_preview(*[f"line {index}" for index in range(30)])
    preview.expand()
    preview.begin(at=1.0)
    preview.append("fresh")

    assert len(preview.visible()) == 1
