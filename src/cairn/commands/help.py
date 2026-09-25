HELP_TEXT: dict[str, dict[str, str]] = {
    "trace": {
        "usage": "/trace [TRACE_ID]",
        "description": "Show the latest or specified trace.",
        "detail": (
            "Show the trace with the given TRACE_ID.\n"
            "If no TRACE_ID is provided, the last trace will be shown."
        ),
    },
    "help": {
        "usage": "/help [COMMAND]",
        "description": "Show available commands.",
        "detail": (
            "Show help for the given COMMAND.\n"
            "If no COMMAND is provided, a list of available commands will be shown."
        ),
    },
    "exit": {"usage": "/exit", "description": "Exit Cairn."},
    "quit": {"usage": "/quit", "description": "Exit Cairn."},
}


def handle_help(args: list[str] | None = None) -> None:
    if not args:
        print("Available commands:")
        for name, summary in HELP_TEXT.items():
            print(f"  /{name:<5}  {summary['description']}")
        return

    for name in args:
        info = HELP_TEXT.get(name)
        if info:
            print(f"Usage: {info['usage']}")
            print(info.get("detail", info["description"]))
        else:
            print(f"No help available for command: {name}")
