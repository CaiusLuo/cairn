HELP_TEXT: dict[str, dict[str, str]] = {
    "model": {
        "usage": (
            "/model | /model list | /model use <name> | /model add <group> <model-id>"
            " | /model remove <group> <model-id>"
            " | /model move <group> <model-id> <position>"
        ),
        "description": "Show or select a session model, or edit local model IDs.",
        "detail": (
            "In a terminal, /model opens an inline model-group selector.\n"
            "Use Up/Down and Enter to select; Esc or Ctrl+C cancels.\n"
            "Without a terminal, /model prints the current group and candidate IDs.\n"
            "/model list shows primary/fallback IDs (* marks the current group).\n"
            "/model use selects a group by name for subsequent requests.\n"
            "Selection preserves conversation history and does not modify configuration.\n"
            "A listed model that failed recently shows its most recent failure.\n"
            "Models and groups belong to the active provider; /provider switches it.\n"
            "/model add appends an ID to an existing group in .cairn/models.toml.\n"
            "/model remove deletes an existing ID, but cannot remove a group's final ID.\n"
            "/model move reorders an ID within its group at a 1-based position.\n"
            "Other IDs keep their relative order; a no-op does not rewrite the file.\n"
            "In a [[providers]] catalog only the active provider's group is edited.\n"
            "Edits preserve comments; unsupported move formatting is rejected.\n"
            "Restart and normal provider approval\n"
            "are required to load changes; the current session is not reloaded.\n"
            "Create TOML first if using legacy .env configuration."
        ),
    },
    "provider": {
        "usage": "/provider | /provider list | /provider use <name>",
        "description": "Show or switch the active provider.",
        "detail": (
            "Show the active provider, its endpoint, credential variable and\n"
            "current model group.\n"
            "/provider list lists configured providers (* marks the active one).\n"
            "/provider use displays the target endpoint, credential variable and\n"
            "models, then requires explicit approval before resolving its credential.\n"
            "Switching transfers the conversation history to the new provider,\n"
            "starts at its first model group and resets session tool-permission\n"
            "grants; history, workspace and traces are preserved.\n"
            "A denied, unavailable or failed switch keeps the current provider."
        ),
    },
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
