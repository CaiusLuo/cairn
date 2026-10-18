"""The status line reports only what a turn actually measured."""

import pytest

from cairn.terminal import output as ui


def render(**kwargs: object) -> str:
    return ui.format_status_line(**kwargs).plain  # type: ignore[arg-type]


def test_absent_signals_are_omitted_rather_than_placeheld() -> None:
    """A figure the turn did not measure must not appear as a dash."""
    rendered = render(active=True)

    assert "tok/s" not in rendered
    assert "ctx" not in rendered
    assert "cached" not in rendered


def test_a_zero_cache_fraction_is_not_reported() -> None:
    """Zero hits means the feature did not apply, not that it failed."""
    assert "cached" not in render(active=True, cached_fraction=0.0)


def test_measured_signals_appear_together() -> None:
    rendered = render(
        active=True,
        tokens_per_second=42.4,
        context_fraction=0.34,
        cached_fraction=0.68,
        reasoning_tokens=76,
    )

    assert "42 tok/s" in rendered
    assert "34%" in rendered
    assert "68% cached" in rendered
    assert "76 reasoning" in rendered


def test_the_spinner_and_the_bar_carry_information_without_color() -> None:
    """Colour is never the only signal, so the glyphs differ between states."""
    streaming = render(active=True, context_fraction=0.5)
    finished = render(active=False, context_fraction=0.5)

    assert streaming.startswith(ui.GLYPHS.spinner)
    assert finished.startswith(ui.GLYPHS.tick)
    assert ui.GLYPHS.bar_full in streaming and ui.GLYPHS.bar_empty in streaming


@pytest.mark.parametrize("fraction", [-1.0, 0.0, 0.5, 1.0, 2.0])
def test_the_bar_is_bounded(fraction: float) -> None:
    """A fraction outside 0..1 must not produce a bar of the wrong width."""
    rendered = render(active=True, context_fraction=fraction)
    bar = rendered.split("ctx ", 1)[1].split(" ", 1)[0]

    assert len(bar) == ui._BAR_WIDTH


def test_ascii_glyphs_are_available_for_a_plain_terminal() -> None:
    assert ui.ASCII_GLYPHS.bar_full.isascii()
    assert ui.ASCII_GLYPHS.spinner.isascii()
    assert ui.ASCII_GLYPHS.reasoning.isascii()
