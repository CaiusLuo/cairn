"""The two-line status area under the input.

Two lines because they answer different questions and age differently: what the
session talks to changes when the operator switches, while the numbers change every
second. The rules here are the ones a reader notices: no fake zeros, no rate that
keeps decaying after the turn, and the model never disappears.
"""

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

    assert "openai/x" in first
    assert "bailian" in first


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

    assert "312 in" in second
    assert "58 out" in second


def test_context_usage_appears_when_known() -> None:
    display = _display(model="openai/x")
    display.observe_context(used=18, limit=100)

    _, second = display.status_lines()

    assert "ctx 18%" in second


def test_the_rate_stops_moving_when_the_turn_ends() -> None:
    """A live rate would decay with the idle clock, reading as a slowdown."""
    display = _display(model="openai/x")
    display.observe_step_usage(input_tokens=100, output_tokens=200)
    display._frozen_rate = 40.0

    first = display.status_lines()[1]
    second = display.status_lines()[1]

    assert first == second
    assert "40 tok/s" in first


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

    assert "openai/x" in first


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

    assert any("312 in" in text for text in painted)


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

    assert any("openai/x" in text and "bailian" in text for text in painted)
