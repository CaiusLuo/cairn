HELP_TEXT: dict[str, dict[str, str]] = {
    "trace": {
        "usage": (
            "/trace | /trace TRACE_ID | /trace list [N] | /trace count "
            "| /trace del TRACE_ID | /trace del --tail N"
        ),
        "description": "Show, count, or delete stored traces.",
        "detail": (
            "Show the trace with the given TRACE_ID (full ID or unique prefix).\n"
            "If no TRACE_ID is provided, the last trace will be shown.\n"
            "/trace list shows the 10 most recent completed traces.\n"
            "/trace list N shows the N most recent completed traces (1 <= N <= 100).\n"
            "/trace count prints how many traces are stored.\n"
            "/trace del TRACE_ID deletes that trace.\n"
            "/trace del --tail N deletes the N oldest traces, i.e. the end of\n"
            "/trace list. Traces that cannot be read are skipped with a warning."
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
