from cairn.commands.context import CommandContext
from cairn.trace_ui import print_trace, print_trace_list

TRACE_LIST_LIMIT = 10
TRACE_LIST_USAGE = "Usage: /trace list [N] (1 <= N <= 100)"


def handle_trace(context: CommandContext, args: list[str]) -> None:
    list_traces = bool(args and args[0] == "list")

    try:
        if list_traces:
            if len(args) > 2:
                raise ValueError(TRACE_LIST_USAGE)
            try:
                limit = int(args[1]) if len(args) == 2 else TRACE_LIST_LIMIT
            except ValueError:
                raise ValueError(TRACE_LIST_USAGE) from None
            if not 1 <= limit <= 100:
                raise ValueError(TRACE_LIST_USAGE)
            result = context.trace_reader.list_traces(limit=limit)
        else:
            trace_id = args[0] if args else context.last_trace_id
            if trace_id is None:
                print("No trace available yet.")
                return
            spans = context.trace_reader.read(trace_id)
    except (OSError, ValueError) as exc:
        print(exc)
        return

    if list_traces:
        print_trace_list(result)
    else:
        print_trace(spans)
