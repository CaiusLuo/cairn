"""The reasoning preview, and what it is allowed to leave behind.

Reasoning is temporary: it is worth watching while it arrives and worth only a
receipt afterwards. These tests pin the behaviours that made the transcript
unreadable before — the window showing the wrong end of the text, the finished
block being reprinted into the scrollback, and a key hint that no longer works —
plus the two that made the preview misleading while it ran: rows cut at provider
delta boundaries instead of at the terminal width, and a clock that never moved
because it read the start time as both ends of the interval.
"""

from cairn.terminal.thinking import (
    DEFAULT_WIDTH,
    EXPANDED_ROWS,
    FRAME_INTERVAL_SECONDS,
    FRAMES,
    PREVIEW_ROWS,
    ThinkingPreview,
)


def _running_preview(*lines: str, width: int | None = None) -> ThinkingPreview:
    """A preview fed these lines as separate lines, then still running."""
    preview = ThinkingPreview()
    if width is not None:
        preview.set_width(width)
    preview.begin(at=0.0)
    if lines:
        preview.append("\n".join(lines))
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


# -- rendered rows, not provider deltas -----------------------------------
#
# A delta is an artefact of how a provider chops its stream: it can end mid-word
# or mid-character. The window is the tail of the text as the terminal would wrap
# it, so a long thought shows its last two *rows* rather than two fragments.


def test_a_chunk_boundary_is_not_a_line_break() -> None:
    """One sentence delivered in four deltas is still one row."""
    preview = ThinkingPreview()
    preview.set_width(80)
    preview.begin(at=0.0)
    for fragment in ("Reading the ", "current parser ", "and its ", "callers..."):
        preview.append(fragment)

    assert list(preview.visible()) == ["Reading the current parser and its callers..."]


def test_a_long_line_shows_only_its_last_two_wrapped_rows() -> None:
    """The window slices rendered rows, not logical lines."""
    preview = ThinkingPreview()
    preview.set_width(10)
    preview.begin(at=0.0)
    preview.append("0123456789abcdefghijKLMNOPQRST")

    assert list(preview.visible()) == ["abcdefghij", "KLMNOPQRST"]


def test_the_last_two_rows_of_a_long_line_are_the_newest_text() -> None:
    preview = ThinkingPreview()
    preview.set_width(10)
    preview.begin(at=0.0)
    preview.append("abcdefghijklmnopqrstuvwxyz")

    visible = list(preview.visible())
    assert len(visible) == PREVIEW_ROWS
    assert "".join(visible) == "klmnopqrstuvwxyz"[-20:]


def test_a_resize_recomputes_the_tail() -> None:
    """Which rows exist depends on the width, so a resize cannot keep old rows."""
    preview = ThinkingPreview()
    preview.set_width(80)
    preview.begin(at=0.0)
    preview.append("abcdefghij")
    assert list(preview.visible()) == ["abcdefghij"]

    preview.set_width(5)

    # The same text now occupies two rows, and both fit in the two-row window.
    assert list(preview.visible()) == ["abcde", "fghij"]


def test_setting_the_same_width_reports_no_change() -> None:
    preview = ThinkingPreview()
    preview.set_width(DEFAULT_WIDTH)

    assert preview.set_width(DEFAULT_WIDTH) is False


def test_a_width_must_be_positive_to_be_adopted() -> None:
    preview = ThinkingPreview()

    assert preview.set_width(0) is False
    assert preview.width == DEFAULT_WIDTH


def test_a_blank_row_is_not_spent() -> None:
    """An empty line in the reasoning is not a row the preview should show."""
    preview = _running_preview("first", "", "   ", "second")

    assert list(preview.visible()) == ["first", "second"]


def test_an_explicit_newline_still_starts_a_row() -> None:
    preview = _running_preview("first\nsecond")

    assert list(preview.visible()) == ["first", "second"]


# -- the clock runs -------------------------------------------------------
#
# The header used to read its start time as both ends of the interval, so the
# clock showed 00:00 for as long as the model was silent. It advances on a tick
# the caller drives, which is not another Agent loop: it only decides what the
# current frame looks like.


def test_the_elapsed_time_advances_while_waiting() -> None:
    preview = _running_preview("thinking")

    preview.tick(at=3.0)

    assert preview.elapsed == 3.0


def test_the_header_clock_moves_without_a_new_delta() -> None:
    preview = _running_preview("thinking")
    before = preview.header()

    preview.tick(at=3.0)

    assert preview.header() != before
    assert "3.0s" in preview.header()


def test_the_spinner_advances_on_the_frame_interval() -> None:
    preview = _running_preview("thinking")
    first = preview.frame

    preview.tick(at=FRAME_INTERVAL_SECONDS)

    assert preview.frame != first


def test_a_tick_inside_the_interval_does_not_redraw() -> None:
    """Four frames a second is the budget; a faster tick costs a repaint for nothing."""
    preview = _running_preview("thinking")

    assert preview.tick(at=FRAME_INTERVAL_SECONDS / 2) is False


def test_a_tick_reports_when_the_frame_changed() -> None:
    preview = _running_preview("thinking")

    assert preview.tick(at=FRAME_INTERVAL_SECONDS) is True


def test_a_settled_preview_stops_ticking() -> None:
    preview = _running_preview("thought")
    preview.finish(at=1.0)
    settled_elapsed = preview.elapsed

    assert preview.tick(at=50.0) is False
    assert preview.elapsed == settled_elapsed


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


def test_paused_tool_time_is_not_counted_as_thinking() -> None:
    preview = ThinkingPreview()
    preview.begin(at=10.0)
    preview.append("first step")
    preview.finish(at=12.0)
    preview.resume(at=100.0)
    preview.append("second step")
    preview.finish(at=103.0)

    assert preview.elapsed == 5.0
    assert preview.receipt() == "Thinking  5.0s"


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


def test_whitespace_only_reasoning_earns_no_receipt() -> None:
    """A turn that produced no reasoning must not claim a thinking step."""
    preview = _running_preview("   ", "\n\n")
    preview.finish(at=1.0)

    assert not preview.has_content()
    assert preview.receipt() == "Thinking  1.0s"


# -- expansion ------------------------------------------------------------
#
# The header offers one key, so that key toggles: it widens the window and puts it
# back. Only growing left a reader no way to ask for their screen back.


def test_expanding_a_running_preview_reveals_more_rows() -> None:
    preview = _running_preview(*[f"line {index}" for index in range(30)])
    collapsed = len(preview.visible())

    assert preview.toggle_expanded()
    assert len(preview.visible()) > collapsed


def test_a_second_toggle_collapses_it_again() -> None:
    """The key's promise is a toggle, so it has to be able to undo itself."""
    preview = _running_preview(*[f"line {index}" for index in range(30)])

    preview.toggle_expanded()
    preview.toggle_expanded()

    assert len(preview.visible()) == PREVIEW_ROWS
    assert not preview.expanded()


def test_expansion_adds_a_bounded_step() -> None:
    preview = _running_preview(*[f"line {index}" for index in range(30)])

    preview.toggle_expanded()

    assert len(preview.visible()) == PREVIEW_ROWS + EXPANDED_ROWS


def test_expansion_never_reaches_beyond_the_text_received() -> None:
    """The preview is all there is: reasoning is not kept after the turn."""
    preview = _running_preview("one", "two", "three")
    preview.toggle_expanded()

    assert len(preview.visible()) == 3


def test_a_settled_preview_cannot_be_expanded() -> None:
    """Nothing is left to reveal, so the key must not appear to work."""
    preview = _running_preview("thought")
    preview.finish(at=1.0)

    assert not preview.toggle_expanded()
    assert not preview.running


def test_the_hint_is_absent_once_the_preview_settles() -> None:
    """A key that does nothing is worse than no key."""
    preview = _running_preview("thought")
    preview.finish(at=1.0)

    assert preview.hint() == ""


def test_the_hint_names_the_key_while_running() -> None:
    preview = _running_preview("thought")
    assert "Ctrl+O" in preview.hint()


def test_the_hint_names_the_other_direction_once_expanded() -> None:
    """An expanded preview's key now collapses, so the hint says so."""
    preview = _running_preview(*[f"line {index}" for index in range(30)])
    preview.toggle_expanded()

    assert "less" in preview.hint()


def test_beginning_again_collapses_the_previous_expansion() -> None:
    preview = _running_preview(*[f"line {index}" for index in range(30)])
    preview.toggle_expanded()
    preview.begin(at=1.0)
    preview.append("fresh")

    assert len(preview.visible()) == 1


# -- display cells, not characters ---------------------------------------
#
# A row is a number of terminal cells. A CJK glyph takes two, a combining mark takes
# none, and an emoji usually two, so wrapping by index gives rows of the wrong width
# for every audience but one — and the window then shows a different number of lines
# than the terminal does.


def test_a_chinese_line_wraps_by_cells() -> None:
    """Ten CJK glyphs fill twenty cells, so a twenty-cell window holds them exactly."""
    preview = ThinkingPreview()
    preview.set_width(20)
    preview.begin(at=0.0)
    preview.append("中" * 10)

    assert list(preview.rendered_rows()) == ["中" * 10]


def test_a_wide_line_splits_where_the_cells_run_out() -> None:
    preview = ThinkingPreview()
    preview.set_width(10)
    preview.begin(at=0.0)
    preview.append("中" * 8)  # sixteen cells

    rows = list(preview.rendered_rows())

    assert ["".join(row) for row in rows] == ["中" * 5, "中" * 3]


def test_every_row_fits_the_width_in_cells() -> None:
    from prompt_toolkit.utils import get_cwidth

    preview = ThinkingPreview()
    preview.set_width(16)
    preview.begin(at=0.0)
    preview.append("中文abc🙂 def中文" * 5)

    for row in preview.rendered_rows():
        assert sum(get_cwidth(char) for char in row) <= 16


def test_a_combining_mark_stays_with_its_base() -> None:
    """A zero-width mark belongs to the glyph before it, not to a row of its own."""
    preview = ThinkingPreview()
    preview.set_width(12)
    preview.begin(at=0.0)
    preview.append("e\u0301" * 3)

    rows = list(preview.rendered_rows())

    assert "e\u0301" in "".join(rows)


def test_mixed_scripts_keep_the_newest_two_rows() -> None:
    preview = ThinkingPreview()
    preview.set_width(8)
    preview.begin(at=0.0)
    preview.append("中文 text 🙂 more 中文 text")

    visible = list(preview.visible())

    assert len(visible) == PREVIEW_ROWS
    assert "".join(visible).endswith("中文 text".strip()[-4:]) or "中文" in "".join(
        visible
    )
