HELP_TEXT = {
    "exit": ("Usage: /exit", "Exit Cairn."),
    "quit": ("Usage: /quit", "Exit Cairn."),
    "trace": (
        "Usage: /trace [TRACE_ID]",
        "Show the trace with the given TRACE_ID.",
        "If no TRACE_ID is provided, the last trace will be shown.",
    ),
    "help": (
        "Usage: /help [COMMAND]",
        "Show help for the given COMMAND.",
        "If no COMMAND is provided, a list of available commands will be shown.",
    ),
}


def handle_help(args: list[str] | None = None) -> None:
    if not args:
        print("Available commands:")
        for cmd in HELP_TEXT:
            print(f"  /{cmd} - {HELP_TEXT[cmd][0]}")
        return

    for name in args:
        lines = HELP_TEXT.get(name)
        if lines:
            print("\n".join(lines))
        else:
            print(f"No help available for command: {name}")
