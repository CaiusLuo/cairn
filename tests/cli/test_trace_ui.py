from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from rich.console import Console

import cairn.terminal.trace_output as trace_ui
from cairn.observability.models import (
    Span,
    SpanStatus,
    TraceContext,
    TraceListResult,
)


@pytest.mark.parametrize(
    ("attributes", "display_name"),
    [
        ({"tool": "bash"}, "bash"),
        ({"tool": "custom_tool"}, "custom_tool"),
        ({}, "tool.execute"),
        ({"tool": ""}, "tool.execute"),
        ({"tool": None}, "tool.execute"),
        ({"tool": 42}, "tool.execute"),
    ],
)
def test_print_trace_renders_tool_display_name_safely(
    monkeypatch: pytest.MonkeyPatch,
    attributes: dict[str, object],
    display_name: str,
) -> None:
    started = datetime(2026, 9, 1, tzinfo=UTC)
    root = Span(
        context=TraceContext(trace_id="trace", span_id="root"),
        name="agent.turn",
        start_time=started,
        end_time=started + timedelta(seconds=1),
        status=SpanStatus.OK,
    )
    child = Span(
        context=TraceContext(trace_id="trace", span_id="child", parent_span_id="root"),
        name="tool.execute",
        start_time=started + timedelta(milliseconds=100),
        attributes=attributes,
    )
    output = StringIO()
    monkeypatch.setattr(
        trace_ui,
        "console",
        Console(file=output, force_terminal=False, color_system=None),
    )

    trace_ui.print_trace([root, child])
    rendered = output.getvalue()

    assert "agent.turn" in rendered
    assert display_name in rendered
    assert "[running]" in rendered
    if display_name != "tool.execute":
        assert "tool.execute" not in rendered
    assert child.name == "tool.execute"


def test_print_trace_list_caps_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = StringIO()
    monkeypatch.setattr(
        trace_ui,
        "console",
        Console(file=output, force_terminal=False, color_system=None),
    )
    result = TraceListResult(
        diagnostics=[f"skipped corrupt trace {index:08x}" for index in range(9)]
    )

    trace_ui.print_trace_list(result)
    rendered = output.getvalue()

    assert rendered.count("warning: skipped corrupt trace") == trace_ui.MAX_DIAGNOSTICS
    assert "warning: ... and 4 more" in rendered
