"""The visual grammar of the transcript.

A conversation reads as one thread when the left edge is a rhythm: a glyph starts
each turn, continuation lines align under it, and no word announces who is
speaking. These tests pin that grammar, because every part of it is invisible to a
passing test suite and obvious to a reader when it breaks.

The role label this replaced (`Cairn>` on its own line) is asserted absent: none of
the mature agents print one, and a rendered role marker is treated as a defect
rather than a feature.
"""

import io

import pytest
from rich.console import Console

from cairn.terminal import output as ui


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buffer = io.StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=buffer, color_system=None, force_terminal=False, width=60),
    )
    return buffer


def test_a_user_message_opens_with_the_glyph(rendered: io.StringIO) -> None:
    ui.print_user_message("what does the parser do?")

    first = rendered.getvalue().splitlines()[0]
    assert first.startswith(f"{ui.USER_GLYPH} ")
    assert "what does the parser do?" in first


def test_a_reply_opens_with_its_glyph(rendered: io.StringIO) -> None:
    ui.print_assistant_response("It builds a tree.")

    lines = [line for line in rendered.getvalue().splitlines() if line.strip()]
    assert lines[0].startswith(f"{ui.ASSISTANT_GLYPH} ")
    assert "It builds a tree." in lines[0]


def test_the_glyph_is_on_the_first_line_only(rendered: io.StringIO) -> None:
    """A marker on every line would be copied along with selected code."""
    ui.print_assistant_response("first paragraph\n\nsecond paragraph")

    body = [line for line in rendered.getvalue().splitlines() if line.strip()]
    assert body[0].count(ui.ASSISTANT_GLYPH) == 1
    for line in body[1:]:
        assert ui.ASSISTANT_GLYPH not in line


def test_continuation_lines_align_under_the_gutter(rendered: io.StringIO) -> None:
    """The left edge must stay one straight column."""
    ui.print_assistant_response("a paragraph long enough to wrap onto another line")

    body = [line for line in rendered.getvalue().splitlines() if line.strip()]
    for line in body[1:]:
        assert line.startswith(" " * ui.GUTTER)


def test_no_role_label_is_printed(rendered: io.StringIO) -> None:
    """A word at column zero turns the transcript into a table of contents."""
    ui.print_user_message("hello")
    ui.print_assistant_response("hi")

    assert "Cairn>" not in rendered.getvalue()


def test_a_user_message_is_separated_from_the_reply(rendered: io.StringIO) -> None:
    ui.print_user_message("hello")
    ui.print_assistant_response("hi")

    lines = rendered.getvalue().splitlines()
    assert "" in lines


def test_turn_blocks_have_one_blank_row_in_both_directions(
    rendered: io.StringIO,
) -> None:
    ui.print_user_message("first question")
    ui.print_assistant_response("first answer")
    ui.print_turn_separator()
    ui.print_user_message("second question")

    lines = rendered.getvalue().splitlines()
    first_user = next(i for i, line in enumerate(lines) if "first question" in line)
    first_reply = next(i for i, line in enumerate(lines) if "first answer" in line)
    second_user = next(i for i, line in enumerate(lines) if "second question" in line)
    assert lines[first_reply - 1] == ""
    assert lines[second_user - 1] == ""
    assert sum(not line.strip() for line in lines[first_user + 1 : first_reply]) == 1
    assert sum(not line.strip() for line in lines[first_reply + 1 : second_user]) == 1


def test_a_multiline_user_message_indents_its_continuations(
    rendered: io.StringIO,
) -> None:
    ui.print_user_message("first line\nsecond line")

    lines = rendered.getvalue().splitlines()
    assert lines[0].startswith(ui.USER_GLYPH)
    assert lines[1].startswith(" " * ui.GUTTER)
    assert ui.USER_GLYPH not in lines[1]


def test_the_gutter_is_two_columns() -> None:
    """Two is the width every surveyed agent uses, and it keeps the edge straight."""
    assert ui.GUTTER == 2


def test_the_glyphs_are_ascii() -> None:
    """A width-ambiguous glyph breaks alignment on a terminal that renders it wide."""
    assert ui.USER_GLYPH.isascii()
    assert ui.ASSISTANT_GLYPH.isascii()
