import pytest

from cairn.commands.help import handle_help


def test_help_lists_command_descriptions(capsys: pytest.CaptureFixture[str]) -> None:
    handle_help()

    assert capsys.readouterr().out.splitlines() == [
        "Available commands:",
        "  /trace  Show the latest or specified trace.",
        "  /help   Show available commands.",
        "  /exit   Exit Cairn.",
        "  /quit   Exit Cairn.",
    ]


def test_help_keeps_detailed_usage(capsys: pytest.CaptureFixture[str]) -> None:
    handle_help(["trace"])

    assert capsys.readouterr().out.splitlines() == [
        "Usage: /trace [TRACE_ID]",
        "Show the trace with the given TRACE_ID.",
        "If no TRACE_ID is provided, the last trace will be shown.",
    ]
