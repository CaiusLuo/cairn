from cairn.commands.context import CommandContext
from cairn.trace_ui import print_trace, print_trace_list


def handle_trace(context: CommandContext, args: list[str]) -> None:
    list_traces = bool(args and args[0] == "list")

    try:
        if list_traces:
            spans = context.trace_reader.list_traces()
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
        print_trace_list(spans)
    else:
        print_trace(spans)
