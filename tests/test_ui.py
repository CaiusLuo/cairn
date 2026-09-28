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
                "tool": "bash",
                "exit_code": 0,
                "stdout": "workspace\n",
                "stderr": "warning\n",
            },
        ),
        Event(type="tool_error", data={"tool": "bash", "error": "failed"}),
        Event(type="agent_finish"),
        Event(
            type="agent_budget_exhausted",
            data={"reason": "max_steps", "limit": 3, "used": 3},
        ),
        Event(
            type="trace_finish",
            data={"trace_id": "0123456789abcdef", "status": "ok"},
        ),
    ]

    for event in events:
        ui.console_event_handler(event)

    rendered = output.getvalue()
    for expected in (
        "trace: 0123456789abcdef",
        "step 1/3",
        "→ bash: pwd",
        "✓ exit 0",
        "✗ bash: failed",
        "✓ done",
        "Agent stopped: step budget exhausted (3/3).",
    ):
        assert expected in rendered
    assert "workspace" not in rendered
    assert "warning" not in rendered
    assert rendered.count("trace:") == 1
    assert rendered.rstrip().endswith("trace: 0123456789abcdef (ok)")


def test_console_event_handler_reports_trace_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)

    ui.console_event_handler(
        Event(
            type="trace_finish",
            data={
                "trace_id": "failed-trace",
                "status": "ok",
                "persisted": False,
                "persistence_error": "OSError: simulated trace write failure",
            },
        )
    )

    rendered = output.getvalue()
    assert "trace unavailable: persistence failed" in rendered
    assert "OSError: simulated trace write failure" in rendered
    assert "trace: failed-trace" not in rendered


def test_compact_read_file_rendering_hides_file_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    file_contents = "VERY-LONG-FILE-CONTENTS\n" * 200

    ui.console_event_handler(
        Event(
            type="tool_call",
            data={
                "tool": "read_file",
                "arguments": {
                    "path": "src/cairn/core/loop.py",
                    "start_line": 1,
                    "end_line": 200,
                },
            },
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool": "read_file",
                "exit_code": 0,
                "stdout": file_contents,
                "stderr": "",
            },
        )
    )

    rendered = output.getvalue()
    assert "→ read_file src/cairn/core/loop.py lines 1-200" in rendered
    assert "✓ read_file" in rendered
    assert "VERY-LONG-FILE-CONTENTS" not in rendered


def test_compact_edit_file_rendering_hides_replacement_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    secret_payload = "VERY-LONG-CODE-PAYLOAD"

    ui.console_event_handler(
        Event(
            type="tool_call",
            data={
                "tool": "edit_file",
                "arguments": {
                    "path": "a.py",
                    "old_text": secret_payload,
                    "new_text": secret_payload,
                },
            },
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool": "edit_file",
                "exit_code": 0,
                "stdout": "Updated a.py",
                "stderr": "",
            },
        )
    )

    rendered = output.getvalue()
    assert "→ edit_file a.py" in rendered
    assert "✓ Updated a.py" in rendered
    assert secret_payload not in rendered


def test_compact_bash_success_hides_output(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _capture_console(monkeypatch)
    command = "uv run pytest -q"
    large_stdout = "VERY-LARGE-PYTEST-OUTPUT\n" * 1_000

    ui.console_event_handler(
        Event(
            type="tool_call",
            data={"tool": "bash", "arguments": {"command": command}},
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool": "bash",
                "exit_code": 0,
                "stdout": large_stdout,
                "stderr": "warning that stays compact",
            },
        )
    )

    rendered = output.getvalue()
    assert f"→ bash: {command}" in rendered
    assert "✓ exit 0" in rendered
    assert "VERY-LARGE-PYTEST-OUTPUT" not in rendered
    assert "warning that stays compact" not in rendered


def test_compact_bash_failure_shows_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _capture_console(monkeypatch)

    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool": "bash",
                "exit_code": 1,
                "stdout": "stdout should not replace stderr",
                "stderr": "pytest collection failed",
            },
        )
    )

    rendered = output.getvalue()
    assert "✗ exit 1" in rendered
    assert "pytest collection failed" in rendered
    assert "stdout should not replace stderr" not in rendered


def test_compact_bash_failure_falls_back_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)

    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool": "bash",
                "exit_code": 2,
                "stdout": "fallback diagnostic",
                "stderr": "",
            },
        )
    )

    rendered = output.getvalue()
    assert "✗ exit 2" in rendered
    assert "fallback diagnostic" in rendered


def test_compact_tool_fields_are_rendered_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    command = "printf '[/bold]'"

    ui.console_event_handler(
        Event(
            type="tool_call",
            data={"tool": "bash", "arguments": {"command": command}},
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool": "bash",
                "exit_code": 1,
                "stdout": "",
                "stderr": "[/dim]",
            },
        )
    )
    ui.console_event_handler(
        Event(type="tool_error", data={"tool": "edit_file", "error": "[/bold red]"})
    )

    rendered = output.getvalue()
    assert command in rendered
    assert "[/dim]" in rendered
    assert "[/bold red]" in rendered


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
    confirm_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def confirm_yes(*args: object, **kwargs: object) -> bool:
        confirm_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(Confirm, "ask", confirm_yes)
    command = "printf '[/bold]'"
    tool_call = ToolCall(
        id="call-1",
        name="bash",
        arguments={"command": command},
    )

    assert ui.console_permission_handler(tool_call) == PermissionResult(
        policy_decision=PermissionDecision.ASK,
        allowed=True,
        prompted=True,
    )
    assert "Command: printf '[/bold]'" in output.getvalue()
    assert confirm_calls == [(("Allow this action?",), {"default": False})]


def test_console_permission_handler_can_deny_other_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)

    def confirm_no(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(Confirm, "ask", confirm_no)
    tool_call = ToolCall(
        id="call-1",
        name="other[/bold]",
        arguments={"value": 1},
    )

    assert ui.console_permission_handler(tool_call) == PermissionResult(
        policy_decision=PermissionDecision.ASK,
        allowed=False,
        prompted=True,
    )
    assert "Tool: other[/bold]" in output.getvalue()
    assert "Arguments: {'value': 1}" in output.getvalue()
