from cairn.interfaces.cli.commands.catalog import CATALOG, CommandEntry
from cairn.interfaces.cli.commands.compact import (
    COMPACT_DETAIL,
    COMPACT_SUMMARY,
    COMPACT_USAGE,
)
from cairn.interfaces.cli.commands.model import MODEL_DETAIL, MODEL_SUMMARY, MODEL_USAGE
from cairn.interfaces.cli.commands.provider import (
    PROVIDER_DETAIL,
    PROVIDER_SUMMARY,
    PROVIDER_USAGE,
)
from cairn.interfaces.cli.commands.trace import TRACE_DETAIL, TRACE_SUMMARY, TRACE_USAGE

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
}


def _entry_help(entry: CommandEntry) -> str:
    info = HELP_TEXT.get(entry.name.lstrip("/"), {})
    usage = str(info.get("usage") or entry.usage or entry.name)
    detail = str(info.get("detail") or entry.detail or entry.summary)
    lines: list[str] = [f"Usage: {usage}", detail]
    if entry.children:
        lines.append("Subcommands:")
        lines.extend(f"  {child.name:<12} {child.summary}" for child in entry.children)
    return "\n".join(lines)


def print_available_commands() -> None:
    print("Available commands:")
    for entry in CATALOG.roots:
        print(f"  {entry.name:<10} {entry.summary}")
        if entry.children:
            print(
                f"{'':<12}subcommands: "
                + ", ".join(child.name for child in entry.children)
            )
    print("Run /help COMMAND [SUBCOMMAND] for details, e.g. /help wx disconnect.")


def handle_help(args: list[str] | None = None) -> None:
    if not args:
        print_available_commands()
        return
    path = tuple(part.lstrip("/") for part in args)
    entry = CATALOG.find(path)
    if entry is None:
        print(f"No help available for command: {' '.join(args)}")
        print_available_commands()
        return
    print(_entry_help(entry))
