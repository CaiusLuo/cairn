import sys
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
from cairn.llm.model_manager import ModelConfig, ProviderConfig
from cairn.llm.provider_catalog import NamedProvider


def _capture_console(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    output = StringIO()
    monkeypatch.setattr(
        ui,
        "console",
        Console(file=output, color_system=None, force_terminal=False, width=200),
    )
    return output


def _provider(
    name: str = "bailian",
    *,
    base_url: str = "https://example.test/v1",
    api_key_env: str = "BAILIAN_API_KEY",
) -> NamedProvider:
    return NamedProvider(
        name=name,
        config=ProviderConfig(
            base_url=base_url,
            api_key_env=api_key_env,
            model_config=(
                ModelConfig("flash", ("openai/qwen-flash", "openai/qwen-flash-backup")),
                ModelConfig("plus", ("openai/qwen-plus",)),
            ),
        ),
    )


def _interactive_stdin(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    terminal = StringIO(answer)
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", terminal)


@pytest.mark.parametrize(
    ("answer", "approved"), [("y\n", True), ("n\n", False), ("\n", False), ("", False)]
)
def test_provider_approval_is_explicit_and_displays_routing(
    monkeypatch: pytest.MonkeyPatch, answer: str, approved: bool
) -> None:
    output = _capture_console(monkeypatch)
    _interactive_stdin(monkeypatch, answer)
    monkeypatch.setenv("BAILIAN_API_KEY", "test-only-secret-never-shown")
    provider = _provider()
    group = provider.config.model_config[0]

    assert ui.confirm_provider_access(provider, group, switching=False) is approved

    rendered = output.getvalue()
    for expected in (
        provider.name,
        provider.config.base_url,
        provider.config.api_key_env,
        group.name,
        "openai/qwen-flash",
        "openai/qwen-flash-backup",
    ):
        assert expected in rendered
    # Only the group being approved is displayed, and never the credential value.
    assert "openai/qwen-plus" not in rendered
    assert "test-only-secret-never-shown" not in rendered
    assert "resets session tool-permission grants" not in rendered


def test_provider_switch_approval_warns_about_history_and_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    _interactive_stdin(monkeypatch, "y\n")
    provider = _provider("local", base_url="http://localhost:8000/v1")
    group = provider.config.model_config[1]

    assert ui.confirm_provider_access(provider, group, switching=True) is True

    rendered = output.getvalue()
    assert provider.name in rendered
    assert provider.config.base_url in rendered
    assert "openai/qwen-plus" in rendered
    assert "conversation history" in rendered
    assert "resets session tool-permission grants" in rendered


def test_provider_approval_rejects_noninteractive_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO("y\n"))
    provider = _provider()

    with pytest.raises(ValueError, match="interactive terminal"):
        ui.confirm_provider_access(
            provider, provider.config.model_config[0], switching=False
        )


def test_choose_provider_lists_candidates_and_requires_a_valid_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    # An unknown name is re-prompted instead of selecting anything.
    _interactive_stdin(monkeypatch, "unknown\nlocal\n")
    providers = (
        _provider("bailian"),
        _provider(
            "local", base_url="http://localhost:8000/v1", api_key_env="LOCAL_KEY"
        ),
    )

    assert ui.choose_provider(providers) is providers[1]

    rendered = output.getvalue()
    for expected in (
        providers[0].name,
        providers[1].name,
        providers[0].config.base_url,
        providers[1].config.base_url,
        providers[0].config.api_key_env,
        providers[1].config.api_key_env,
    ):
        assert expected in rendered


def test_choose_provider_rejects_noninteractive_and_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()

    monkeypatch.setattr(sys, "stdin", StringIO("bailian\n"))
    with pytest.raises(ValueError, match="interactive terminal"):
        ui.choose_provider((provider,))

    _interactive_stdin(monkeypatch, "")
    with pytest.raises(ValueError, match="cancelled"):
        ui.choose_provider((provider,))


def test_choose_model_group_selects_one_configured_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    _interactive_stdin(monkeypatch, "plus\n")
    provider = _provider()

    group = ui.choose_model_group(provider)

    assert group.name == "plus"
    assert group.model_ids == ("openai/qwen-plus",)
    assert "openai/qwen-flash" in output.getvalue()


def test_choose_model_group_rejects_noninteractive_and_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()

    monkeypatch.setattr(sys, "stdin", StringIO("flash\n"))
    with pytest.raises(ValueError, match="interactive terminal"):
        ui.choose_model_group(provider)

    _interactive_stdin(monkeypatch, "")
    with pytest.raises(ValueError, match="cancelled"):
        ui.choose_model_group(provider)


def test_print_provider_selection_shows_provider_group_and_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _capture_console(monkeypatch)
    provider = _provider()
    group = provider.config.model_config[1]

    ui.print_provider_selection(provider, group)

    rendered = output.getvalue()
    assert provider.name in rendered
    assert group.name in rendered
    assert "openai/qwen-plus" in rendered


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
