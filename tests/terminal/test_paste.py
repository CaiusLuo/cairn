"""Collapsing a large paste, and getting its payload back on submit.

A paste is the one input that can arrive many times larger than anything typed, so
it is the one input that must not be shown in full. These tests pin both halves:
when a paste is collapsed, and that the text it stood for still reaches the model.
"""

from cairn.terminal.paste import (
    LARGE_PASTE_CHARACTERS,
    LARGE_PASTE_LINES,
    PasteRegistry,
    decide_paste,
    is_large_paste,
)


def test_a_short_paste_is_left_alone() -> None:
    """Typing a phrase and pasting a phrase should look the same."""
    assert not is_large_paste("fix the test")


def test_a_multi_line_paste_is_large() -> None:
    assert is_large_paste("one\ntwo\nthree")


def test_a_long_single_line_paste_is_large() -> None:
    """A single very long line would still scroll the composer away."""
    assert is_large_paste("x" * (LARGE_PASTE_CHARACTERS + 1))


def test_the_line_threshold_is_three() -> None:
    assert LARGE_PASTE_LINES == 3
    assert is_large_paste("\n".join(["line"] * LARGE_PASTE_LINES))


def test_an_empty_paste_is_not_large() -> None:
    assert not is_large_paste("")


def test_a_large_paste_becomes_a_marker() -> None:
    decision = decide_paste("one\ntwo\nthree")
    assert decision.collapsed
    assert "pasted 3 lines" in decision.marker


def test_a_collapsed_paste_keeps_its_payload() -> None:
    """The payload is what the model must receive, so it cannot be discarded."""
    payload = "one\ntwo\nthree\nfour"
    decision = decide_paste(payload)
    assert decision.payload == payload


def test_a_small_paste_has_no_marker() -> None:
    decision = decide_paste("short")
    assert not decision.collapsed
    assert decision.marker == ""


def test_carriage_returns_are_normalised() -> None:
    """A terminal may paste CRLF; the buffer must not gain stray characters."""
    decision = decide_paste("a\r\nb\r\nc")
    assert "\r" not in decision.payload


def test_a_second_paste_gets_a_distinct_index() -> None:
    first = decide_paste("a\nb\nc", existing=0)
    second = decide_paste("a\nb\nc", existing=1)
    assert first.marker != second.marker
    assert "#2" in second.marker


def test_a_long_single_line_names_its_character_count() -> None:
    decision = decide_paste("x" * (LARGE_PASTE_CHARACTERS + 1))
    assert "chars" in decision.marker


# -- registry -------------------------------------------------------------


def test_a_registered_marker_expands_to_its_payload() -> None:
    registry = PasteRegistry()
    registry.remember("[pasted 3 lines]", "a\nb\nc")
    assert registry.expand("look: [pasted 3 lines]") == "look: a\nb\nc"


def test_text_without_markers_is_unchanged() -> None:
    registry = PasteRegistry()
    assert registry.expand("just text") == "just text"


def test_several_markers_expand_together() -> None:
    registry = PasteRegistry()
    registry.remember("[pasted 3 lines]", "A")
    registry.remember("[pasted 4 lines #2]", "B")
    assert registry.expand("[pasted 3 lines] and [pasted 4 lines #2]") == "A and B"


def test_the_registry_reports_what_it_holds() -> None:
    registry = PasteRegistry()
    assert registry.count == 0
    registry.remember("[pasted 3 lines]", "payload")
    assert registry.count == 1
    assert registry.has("[pasted 3 lines]")


def test_clearing_drops_every_payload() -> None:
    registry = PasteRegistry()
    registry.remember("[pasted 3 lines]", "payload")
    registry.clear()
    assert registry.count == 0
