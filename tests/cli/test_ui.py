from io import StringIO

import pytest
from rich.console import Console
from rich.prompt import Prompt

import cairn.terminal.output as ui
from cairn.core.events import Event
from cairn.core.models import ToolCall
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionRequest,
)


def _capture_console(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    output = StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=output, color_system=None, force_terminal=False, width=200),
    )
    return output


@pytest.mark.parametrize("status", ["ok", "error"])
@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (None, "input unknown, output unknown"),
        ("invalid", "input unknown, output unknown"),
        ({}, "input unknown, output unknown"),
        ({"input_tokens": 30}, "input 30, output unknown"),
        ({"output_tokens": 5}, "input unknown, output 5"),
        ({"input_tokens": 0, "output_tokens": 0}, "input 0, output 0"),
        ({"input_tokens": 30, "output_tokens": 5}, "input 30, output 5"),
        ({"input_tokens": True, "output_tokens": -1}, "input unknown, output unknown"),
        ({"input_tokens": "30", "output_tokens": 1.5}, "input unknown, output unknown"),
    ],
)
def test_trace_footer_renders_known_or_unknown_usage(
    monkeypatch: pytest.MonkeyPatch, usage: object, expected: str, status: str
) -> None:
    output = _capture_console(monkeypatch)
    event = Event(type="trace_finish", data={"trace_id": "trace-id", "status": status})
    if usage is not None:
        event.data["usage"] = usage

    ui.console_event_handler(event)

    assert (
        output.getvalue().strip() == f"trace: trace-id ({status}) · tokens: {expected}"
    )


@pytest.mark.parametrize("persisted", [True, False])
def test_failed_trace_footer_shows_unknown_usage(
    monkeypatch: pytest.MonkeyPatch, persisted: bool
) -> None:
    output = _capture_console(monkeypatch)
    ui.console_event_handler(
        Event(
            type="trace_finish",
            data={
                "trace_id": "failed-trace",
                "status": "error",
                "persisted": persisted,
                "persistence_error": "OSError: write failed",
            },
        )
    )

    rendered = output.getvalue()
    assert rendered.count("tokens: input unknown, output unknown") == 1
    if persisted:
        assert "trace: failed-trace (error)" in rendered
    else:
        assert (
            "trace unavailable: persistence failed (OSError: write failed)" in rendered
        )


def test_context_trimmed_explains_request_omission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    ui.console_event_handler(
        Event(type="context_trimmed", data={"omitted_turns": 2, "omitted_messages": 8})
    )

    assert output.getvalue().strip() == (
        "context: omitted 2 older turns (8 messages); full history retained."
    )


def test_context_trimmed_renders_fields_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    ui.console_event_handler(
        Event(
            type="context_trimmed",
            data={"omitted_turns": "[bold]", "omitted_messages": "[/bold]"},
        )
    )

    assert output.getvalue().strip() == (
        "context: omitted [bold] older turns ([/bold] messages); full history retained."
    )


def test_permission_denial_and_tool_fields_render_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    command = "printf '[/bold]'"

    ui.console_event_handler(
        Event(
            type="tool_call", data={"tool": "bash", "arguments": {"command": command}}
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={"tool": "bash", "exit_code": 1, "stdout": "", "stderr": "[/dim]"},
        )
    )
    ui.console_event_handler(
        Event(type="tool_error", data={"tool": "edit_file", "error": "[/bold red]"})
    )
    ui.console_event_handler(
        Event(
            type="tool_denied",
            data={
                "tool": "bash",
                "error": "sudo is not supported [/bold]",
                "error_type": "PolicyDenied",
            },
        )
    )

    rendered = output.getvalue()
    assert command in rendered
    assert "[/dim]" in rendered
    assert "[/bold red]" in rendered
    assert "denied: sudo is not supported [/bold]" in rendered


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("1", PermissionChoice.ALLOW_ONCE),
        ("2", PermissionChoice.ALLOW_SESSION),
        ("3", PermissionChoice.DENY),
    ],
)
def test_permission_prompt_choices_default_to_deny(
    monkeypatch: pytest.MonkeyPatch, answer: str, expected: PermissionChoice
) -> None:
    output = _capture_console(monkeypatch)

    def choose(*args: object, **kwargs: object) -> str:
        assert kwargs["default"] == "3"
        return answer

    monkeypatch.setattr(Prompt, "ask", choose)
    request = PermissionRequest(
        capability=PermissionCapability.NETWORK,
        justification="test [/dim]",
        tool_call=ToolCall(
            id="1", name="bash", arguments={"command": "printf '[/bold]'"}
        ),
    )

    assert ui.console_permission_prompt(request) == expected
    rendered = output.getvalue()
    assert "Permission required" in rendered
    assert "[1] Allow once" in rendered
    assert "[2] Allow network for this session" in rendered
    assert "[3] Deny" in rendered
    assert "Reason: test [/dim]" in rendered


def test_compact_file_events_hide_contents_and_edit_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    file_contents = "VERY-LONG-FILE-CONTENTS\n" * 200
    edit_payload = "VERY-LONG-CODE-PAYLOAD"

    ui.console_event_handler(
        Event(
            type="tool_call",
            data={
                "tool": "read_file",
                "arguments": {
                    "path": "src/[bold].py",
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
    ui.console_event_handler(
        Event(
            type="tool_call",
            data={
                "tool": "edit_file",
                "arguments": {
                    "path": "src/[bold].py",
                    "old_text": edit_payload,
                    "new_text": edit_payload,
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
                "stdout": "Updated src/[bold].py",
                "stderr": "",
            },
        )
    )

    rendered = output.getvalue()
    assert "→ read_file src/[bold].py lines 1-200" in rendered
    assert "✓ read_file" in rendered
    assert "✓ Updated src/[bold].py" in rendered
    assert "VERY-LONG-FILE-CONTENTS" not in rendered
    assert edit_payload not in rendered


def test_compact_bash_hides_success_and_shows_failure_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    command = "uv run pytest -q"

    ui.console_event_handler(
        Event(
            type="tool_call", data={"tool": "bash", "arguments": {"command": command}}
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool": "bash",
                "exit_code": 0,
                "stdout": "VERY-LARGE-PYTEST-OUTPUT\n" * 100,
                "stderr": "warning stays compact",
            },
        )
    )
    ui.console_event_handler(
        Event(
            type="tool_result",
            data={
                "tool": "bash",
                "exit_code": 1,
                "stdout": "stdout fallback diagnostic",
                "stderr": "pytest collection failed",
            },
        )
    )
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
    assert f"→ bash: {command}" in rendered
    assert "✓ exit 0" in rendered
    assert "VERY-LARGE-PYTEST-OUTPUT" not in rendered
    assert "warning stays compact" not in rendered
    assert "✗ exit 1" in rendered
    assert "pytest collection failed" in rendered
    assert "stdout fallback diagnostic" not in rendered
    assert "✗ exit 2" in rendered
    assert "fallback diagnostic" in rendered
