"""Terminal capability, and the glyph fallbacks every surface depends on.

The status *rendering* this module used to cover now belongs to the two-row footer
under the composer and is asserted in ``test_status_area.py``: the live region no
longer paints a status copy, so there is one owner and one set of tests for it.

What remains here is terminal capability, which is not about spelling anything:
whether an in-place region is safe to draw, and whether the glyphs a terminal is
asked to render have an ASCII fallback.
"""

import sys
from types import SimpleNamespace

import pytest

from cairn.terminal import output as ui


def test_ascii_glyphs_are_available_for_a_plain_terminal() -> None:
    assert ui.ASCII_GLYPHS.reasoning.isascii()


def test_live_display_follows_the_stream_not_a_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirected stream must not be asked to move a cursor it does not have."""
    monkeypatch.setattr(
        ui, "console", SimpleNamespace(is_terminal=False, print=lambda *a, **k: None)
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert ui.supports_live_display() is False

    monkeypatch.setattr(
        ui, "console", SimpleNamespace(is_terminal=True, print=lambda *a, **k: None)
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert ui.supports_live_display() is True


def test_an_interactive_terminal_alone_is_not_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either side reporting a stream is enough to disable in-place drawing."""
    monkeypatch.setattr(
        ui, "console", SimpleNamespace(is_terminal=True, print=lambda *a, **k: None)
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    assert ui.supports_live_display() is False
