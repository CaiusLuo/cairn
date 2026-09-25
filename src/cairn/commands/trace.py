from cairn.commands.context import CommandContext
from cairn.trace_ui import print_trace, print_trace_list


def handle_trace(context: CommandContext, args: list[str]) -> None:
    if args and args[0] == "list":
        spans = context.trace_reader.list_traces()
        print_trace_list(spans)
        return

    trace_id = args[0] if args else context.last_trace_id

    if trace_id is None:
        print("No trace available yet.")
        return

    try:
        spans = context.trace_reader.read(trace_id)
    except (FileNotFoundError, ValueError) as exc:
        print(exc)
        return

    print_trace(spans)
