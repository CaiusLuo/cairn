import sys
from io import StringIO

import pytest
from rich.console import Console
from rich.prompt import Prompt

import cairn.terminal.output as ui
from cairn.core.events import Event
from cairn.core.model_manager import ModelConfig, ProviderConfig
from cairn.core.models import ToolCall
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionRequest,
)
from cairn.core.provider_catalog import NamedProvider


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


def test_a_stored_trace_is_not_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal turn stays quiet: the trace is available through `/trace`."""
    output = _capture_console(monkeypatch)
    ui.console_event_handler(
        Event(
            type="trace_finish",
            data={
                "trace_id": "trace-id",
                "status": "ok",
                "persisted": True,
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        )
    )

    assert output.getvalue() == ""


def test_an_unstored_trace_reports_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one case a reader must act on: the trace could not be stored."""
    output = _capture_console(monkeypatch)
    ui.console_event_handler(
        Event(
            type="trace_finish",
            data={
                "trace_id": "failed-trace",
                "status": "error",
                "persisted": False,
                "persistence_error": "OSError: write failed",
            },
        )
    )

    rendered = output.getvalue()
    assert "trace unavailable: persistence failed (OSError: write failed)" in rendered
    # A turn that never measured tokens says nothing about them: a second
    # unknown beside the one fact that matters is noise, not a diagnostic.
    assert "tokens:" not in rendered


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
