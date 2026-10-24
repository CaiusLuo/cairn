"""How the CLI decides whether a typed line redirects the running turn.

Interrupting and queueing must not share an input: a reader who types a sentence
while work runs is queueing, and only a deliberate marker interrupts. These tests
pin that marker so the two can never be confused for one another.
"""

import pytest

from cairn.terminal.cli import _is_interrupt_request


def test_a_plain_sentence_does_not_interrupt() -> None:
    """Typing while work runs is the common case and must never cancel it."""
    assert not _is_interrupt_request("also check the tests")


def test_a_leading_bang_interrupts() -> None:
    assert _is_interrupt_request("! stop, use the other module")


def test_a_bang_in_the_middle_does_not_interrupt() -> None:
    """The marker is positional, so an exclamation mid-sentence is just text."""
    assert not _is_interrupt_request("this is great! keep going")


def test_a_bang_after_a_space_does_not_interrupt() -> None:
    assert not _is_interrupt_request(" ! leading space")


def test_a_lone_bang_interrupts() -> None:
    assert _is_interrupt_request("!")


def test_an_empty_line_does_not_interrupt() -> None:
    assert not _is_interrupt_request("")


@pytest.mark.parametrize("text", ["!fix", "! fix", "!!"])
def test_any_leading_bang_form_interrupts(text: str) -> None:
    assert _is_interrupt_request(text)
