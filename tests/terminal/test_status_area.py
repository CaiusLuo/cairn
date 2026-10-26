"""The two-line status area under the input.

Two lines because they answer different questions and age differently: what the
session talks to changes when the operator switches, while the numbers change every
second. The rules here are the ones a reader notices: no fake zeros, no rate that
keeps decaying after the turn, and the model never disappears.
"""

import re

import pytest
from prompt_toolkit.utils import get_cwidth

import cairn.terminal.output as output_module
from cairn.core.events import Event
from cairn.terminal.output import StreamingDisplay


def _display(**facts: object) -> StreamingDisplay:
    display = StreamingDisplay()
    if "model" in facts:
        display.observe_model(
            str(facts["model"]), provider=str(facts.get("provider", ""))
        )
    return display


def test_the_status_is_two_lines() -> None:
    lines = _display(model="openai/x", provider="bailian").status_lines()
    assert len(lines) == 2


def test_the_first_line_names_the_model_and_provider() -> None:
    first, _ = _display(model="openai/x", provider="bailian").status_lines()

    assert "◇ x" in first
    assert "openai/" not in first
    assert "bailian" in first


def test_the_status_uses_compact_tokens_and_fits_dock_widths() -> None:
    display = _measured()
    display._frozen_rate = 54.0
    for width in (60, 80, 120):
        lines = display.status_lines(width=width)
        assert len(lines) == 2
        assert all(get_cwidth(line) <= width for line in lines)
        assert "1.2k" in lines[1] and "84" in lines[1]
        assert "54 tok/s" in lines[1]
        assert "18%" in lines[1]
    assert "[" not in display.status_lines(width=40)[1]
    assert "[" in display.status_lines(width=60)[1]


def test_the_ascii_fallback_uses_no_unicode_status_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(output_module, "_declares_unicode", lambda: False)
    display = _measured()
    display._frozen_rate = 54.0
    text = display.status_rendered_text(width=80)

    assert "o " in text
    assert "in " in text and "out " in text and "|" in text
    assert "◇" not in text and "↑" not in text and "↓" not in text
    narrow = display.status_lines(width=24)
    assert all(get_cwidth(line) <= 24 for line in narrow)
    assert "in" in narrow[1] and "out" in narrow[1] and "18%" in narrow[1]
    assert all(line.isascii() for line in narrow)
    assert display.status_rendered_text(width=80).isascii()


def test_an_unmeasured_session_says_ready_rather_than_zero() -> None:
    """Zeros look like a measurement; they are not one."""
    _, second = _display(model="openai/x").status_lines()

    assert second == "ready"
    assert "0 in" not in second
    assert "0 out" not in second


def test_measured_usage_appears_on_the_second_line() -> None:
    display = _display(model="openai/x")
    display.observe_step_usage(input_tokens=312, output_tokens=58)

    _, second = display.status_lines()

    assert "312" in second
    assert "58" in second


def test_context_usage_appears_when_known() -> None:
    display = _display(model="openai/x")
    display.observe_context(used=18, limit=100)

    _, second = display.status_lines()

    assert "ctx " in second and "18%" in second


def test_the_rate_stops_moving_when_the_turn_ends() -> None:
    """A live rate would decay with the idle clock, reading as a slowdown."""
    display = _display(model="openai/x")
    display.observe_step_usage(input_tokens=100, output_tokens=200)
    display._frozen_rate = 40.0

    first = display.status_lines()[1]
    second = display.status_lines()[1]

    assert first == second
    assert "40 tok/s" in second


def test_a_new_turn_clears_the_previous_turn_numbers() -> None:
    display = _display(model="openai/x")
    display.observe_step_usage(input_tokens=100, output_tokens=200)
    display.reset()

    _, second = display.status_lines()

    assert second == "ready"


def test_the_model_survives_a_reset() -> None:
    """Which model this session talks to is a fact about the session, not the turn."""
    display = _display(model="openai/x", provider="bailian")
    display.reset()

    first, _ = display.status_lines()

    assert "◇ x" in first


# -- painting -------------------------------------------------------------
#
# The status has its own destination, so repainting it must not depend on the live
# region being open. Tying the two together meant the final figures — the last
# turn's usage and its frozen rate — never reached the screen, because the live
# region closes exactly when a turn ends and those figures are known.


def _painted() -> tuple[StreamingDisplay, list[str]]:
    painted: list[str] = []
    display = StreamingDisplay(on_status=painted.append)
    return display, painted


def test_the_status_paints_without_a_live_region() -> None:
    """The final figures arrive after the live region has closed."""
    display, painted = _painted()

    display._refresh_status()

    assert painted


def test_the_final_usage_is_painted_after_the_live_region_closes() -> None:
    display, painted = _painted()
    display.observe_step_usage(input_tokens=312, output_tokens=58)
    display._close_live()

    display._refresh_status()

    assert any("312" in text for text in painted)


def test_the_frozen_rate_is_painted_after_the_turn_ends() -> None:
    display, painted = _painted()
    display.observe_step_usage(input_tokens=100, output_tokens=200)
    display._frozen_rate = 42.0

    display._refresh_status()

    assert any("42 tok/s" in text for text in painted)


def test_the_model_survives_into_the_painted_status() -> None:
    display, painted = _painted()
    display.observe_model("openai/x", provider="bailian")

    display._refresh_status()

    assert any("x" in text and "bailian" in text for text in painted)


# -- fitting a narrow terminal -------------------------------------------
#
# A status line that overflows is worse than a shorter one: it wraps into the
# composer or is truncated with no rule about what was dropped. The order below is
# deliberate — the model is shortened before anything is removed, because identity
# matters more than precision; then context goes, because it is available elsewhere;
# usage goes last. The model and provider never disappear together, because a reader
# who cannot tell what they are talking to has lost the most important fact.


def _measured(model: str = "openai/deepseek-v4.1-flash-1017") -> StreamingDisplay:
    display = StreamingDisplay()
    display.observe_model(model, provider="bailian")
    display.observe_step_usage(input_tokens=1200, output_tokens=84)
    display.observe_context(used=23040, limit=128000)
    return display


def test_a_wide_terminal_keeps_everything() -> None:
    first, second = _measured().status_lines(width=100)

    assert "deepseek-v4.1-flash-1017" in first
    assert "bailian" in first
    assert "1.2k" in second
    assert "ctx [" in second and "18%" in second


def test_a_narrow_terminal_shortens_the_model_before_dropping_anything() -> None:
    first, second = _measured().status_lines(width=52)

    assert "1.2k" in second
    assert len(first) <= 52


def test_the_provider_survives_the_first_reduction() -> None:
    """Identity is the last thing to go; a shortened model keeps it readable."""
    first, _ = _measured().status_lines(width=52)
    assert "bailian" in first


def test_a_very_narrow_terminal_drops_rate_and_bar_before_context_percentage() -> None:
    """The numeric context percentage survives after its decorative bar drops."""
    _, second = _measured().status_lines(width=26)

    assert "1.2k" in second
    assert "ctx 18%" in second


def test_no_line_exceeds_the_available_width() -> None:
    for width in (30, 40, 52, 64, 100):
        for line in _measured().status_lines(width=width):
            assert get_cwidth(line) <= width, f"overflowed at width {width}: {line!r}"


def test_the_model_and_provider_never_both_disappear() -> None:
    """A reader who cannot tell what they are talking to has lost the key fact."""
    first, _ = _measured().status_lines(width=24)
    assert first.strip()


# -- facts that must not be throttled ------------------------------------
#
# Ordinary paints are throttled because they repeat. A fact that will never be
# painted again must not be: if it lands inside the window it is dropped, and the
# reader keeps the previous turn's numbers for the rest of the session.


def _painted_at_once(display: StreamingDisplay) -> list[str]:
    painted: list[str] = []
    display._on_status = painted.append
    return painted


def test_the_final_usage_survives_landing_inside_the_throttle_window() -> None:
    display, painted = _painted()
    display._refresh_status()  # a paint just happened
    painted.clear()

    display.observe(
        Event(
            type="trace_finish",
            data={
                "trace_id": "t",
                "usage": {"input_tokens": 312, "output_tokens": 58},
            },
        )
    )

    assert any("312" in text for text in painted)


def test_a_new_identity_is_published_immediately() -> None:
    display, painted = _painted()
    display._refresh_status()
    painted.clear()

    display.observe_model("openai/plus", provider="local")

    assert any("plus" in text and "local" in text for text in painted)


def test_a_resize_is_published_immediately() -> None:
    display, painted = _painted()
    display.observe_model("openai/deepseek-v4.1-flash-1017", provider="bailian")
    display.observe_step_usage(input_tokens=1200, output_tokens=84)
    display._refresh_status()
    painted.clear()

    display.set_status_width(26)

    assert painted
    plain = re.sub(r"\x1b\[[0-9;]*m", "", painted[-1])
    assert all(get_cwidth(line) <= 26 for line in plain.split("\n"))


def test_ordinary_updates_are_still_throttled() -> None:
    display, painted = _painted()
    display._refresh_status()
    painted.clear()

    display._refresh_status()

    assert painted == []
