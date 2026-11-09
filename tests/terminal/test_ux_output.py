"""UX-A output regressions for the CLI streaming surface."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

import cairn.interfaces.cli.output as output_module
from cairn.agent.turn.events import Event


def _display(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[StringIO, output_module.StreamingDisplay]:
    answer = StringIO()
    monkeypatch.setattr(
        output_module,
        "console",
        Console(file=answer, width=200, color_system=None, force_terminal=False),
    )
    monkeypatch.setattr(output_module, "supports_live_display", lambda: False)
    return answer, output_module.StreamingDisplay()


def _feed(display: output_module.StreamingDisplay, chunks: list[str]) -> None:
    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    for chunk in chunks:
        display.observe(Event(type="model_text_delta", data={"text": chunk}))
    display.observe(
        Event(type="model_stream_end", data={"step": 1, "status": "completed"})
    )


def test_completed_markdown_blocks_are_not_repeated_at_stream_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer, display = _display(monkeypatch)
    _feed(display, ["first paragraph\n\n", "second paragraph\n\n", "third paragraph"])
    shown = answer.getvalue()
    assert shown.count("first paragraph") == 1
    assert shown.count("second paragraph") == 1
    assert shown.count("third paragraph") == 1
    assert display.final_text_visible is True


def test_stream_without_deltas_does_not_claim_final_text_was_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer, display = _display(monkeypatch)
    display.observe(Event(type="model_stream_start", data={"step": 1, "attempt": 1}))
    display.observe(
        Event(type="model_stream_end", data={"step": 1, "status": "completed"})
    )
    assert display.final_text_visible is False
