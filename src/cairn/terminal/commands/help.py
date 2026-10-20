from cairn.terminal.commands.compact import (
    COMPACT_DETAIL,
    COMPACT_SUMMARY,
    COMPACT_USAGE,
)
from cairn.terminal.commands.model import MODEL_DETAIL, MODEL_SUMMARY, MODEL_USAGE
from cairn.terminal.commands.provider import (
    PROVIDER_DETAIL,
    PROVIDER_SUMMARY,
    PROVIDER_USAGE,
)
from cairn.terminal.commands.trace import TRACE_DETAIL, TRACE_SUMMARY, TRACE_USAGE

HELP_USAGE = "/help [COMMAND]"
HELP_SUMMARY = "Show available commands."
HELP_DETAIL = (
    "Show help for the given COMMAND.\n"
    "If no COMMAND is provided, a list of available commands will be shown."
)

HELP_TEXT: dict[str, dict[str, str]] = {
    "model": {
        "usage": MODEL_USAGE,
        "description": MODEL_SUMMARY,
        "detail": MODEL_DETAIL,
    },
    "provider": {
        "usage": PROVIDER_USAGE,
        "description": PROVIDER_SUMMARY,
        "detail": PROVIDER_DETAIL,
    },
    "compact": {
        "usage": COMPACT_USAGE,
        "description": COMPACT_SUMMARY,
        "detail": COMPACT_DETAIL,
    },
    "trace": {
        "usage": TRACE_USAGE,
        "description": TRACE_SUMMARY,
        "detail": TRACE_DETAIL,
    },
    "help": {
        "usage": HELP_USAGE,
        "description": HELP_SUMMARY,
        "detail": HELP_DETAIL,
    },
    "exit": {"usage": "/exit", "description": "Exit Cairn."},
    "quit": {"usage": "/quit", "description": "Exit Cairn."},
}


def print_available_commands() -> None:
    print("Available commands:")
    for name, info in HELP_TEXT.items():
        print(f"  /{name:<5}  {info['description']}")
        # Skip a usage line that only repeats the command name.
        if info["usage"] != f"/{name}":
            print(f"{'':<10}usage: {info['usage']}")
    print("Run /help COMMAND for details, e.g. /help trace.")


def handle_help(args: list[str] | None = None) -> None:
    if not args:
        print_available_commands()
        return

    for name in args:
        info = HELP_TEXT.get(name)
        if info:
            print(f"Usage: {info['usage']}")
            print(info.get("detail", info["description"]))
        else:
            print(f"No help available for command: {name}")
            print_available_commands()
