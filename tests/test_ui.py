from io import StringIO

import pytest
from rich.console import Console
from rich.prompt import Confirm

import cairn.ui as ui
from cairn.core.events import Event
from cairn.core.models import ToolCall
from cairn.core.permissions import PermissionDecision, PermissionResult


def _capture_console(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    output = StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=output, color_system=None, force_terminal=False, width=200),
    )
    return output


def test_print_helpers_render_content(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _capture_console(monkeypatch)

    ui.print_banner()
    ui.print_assistant_response("**hello**")

    rendered = output.getvalue()
    assert "Cairn>" in rendered
    assert "hello" in rendered
    assert len(rendered) > 20


def test_console_event_handler_renders_each_event_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    events = [
        Event(type="trace_start", data={"trace_id": "0123456789abcdef"}),
        Event(type="agent_step", data={"step": 1, "max_steps": 3}),
        Event(type="tool_call", data={"tool": "bash", "arguments": {"command": "pwd"}}),
        Event(
            type="tool_result",
            data={
                "exit_code": 0,
                "stdout": "workspace\n",
                "stderr": "warning\n",
            },
        ),
        Event(type="tool_result", data={"exit_code": 0, "stdout": "", "stderr": ""}),
        Event(type="tool_error", data={"tool": "bash", "error": "failed"}),
        Event(type="agent_finish"),
        Event(type="agent_step_limit", data={"max_steps": 3}),
    ]

    for event in events:
        ui.console_event_handler(event)

    rendered = output.getvalue()
    for expected in (
        "trace: 0123456789abcdef",
        "step 1/3",
        "→ bash",
        "workspace",
        "warning",
        "✗ bash: failed",
        "✓ done",
        "Agent stopped after 3 steps.",
    ):
        assert expected in rendered


def test_console_permission_handler_returns_automatic_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_console(monkeypatch)
    tool_call = ToolCall(
        id="call-1",
        name="bash",
        arguments={"command": "pwd"},
    )

    assert ui.console_permission_handler(tool_call) == PermissionResult(
        policy_decision=PermissionDecision.ALLOW,
        allowed=True,
    )


def test_console_permission_handler_prompts_for_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)

    def confirm_yes(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(Confirm, "ask", confirm_yes)
    tool_call = ToolCall(
        id="call-1",
        name="bash",
        arguments={"command": "python -V"},
    )

    assert ui.console_permission_handler(tool_call) == PermissionResult(
        policy_decision=PermissionDecision.ASK,
        allowed=True,
        prompted=True,
    )
    assert "Command: python -V" in output.getvalue()


def test_console_permission_handler_can_deny_other_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)

    def confirm_no(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(Confirm, "ask", confirm_no)
    tool_call = ToolCall(
        id="call-1",
        name="other",
        arguments={"value": 1},
    )

    assert ui.console_permission_handler(tool_call) == PermissionResult(
        policy_decision=PermissionDecision.ASK,
        allowed=False,
        prompted=True,
    )
    assert "Arguments: {'value': 1}" in output.getvalue()
