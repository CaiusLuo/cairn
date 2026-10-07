from cairn.observability.storage import TraceDeleteResult
from cairn.terminal.commands.context import CommandContext
from cairn.terminal.commands.help import handle_help
from cairn.terminal.trace_output import print_trace, print_trace_list

TRACE_LIST_LIMIT = 10
TRACE_LIST_USAGE = "Usage: /trace list [N] (1 <= N <= 100)"
TRACE_COUNT_USAGE = "Usage: /trace count"
TRACE_DELETE_USAGE = "Usage: /trace del TRACE_ID | /trace del --tail N (N >= 1)"


def handle_trace(context: CommandContext, args: list[str]) -> None:
    command = args[0] if args else None

    if command == "list":
        _list_traces(context, args[1:])
    elif command == "count":
        if len(args) > 1:
            print(TRACE_COUNT_USAGE)
            return
        print(f"traces: {context.trace_store.count()}")
    elif command == "del":
        _delete_traces(context, args[1:])
    else:
        _show_trace(context, command)


def _list_traces(context: CommandContext, args: list[str]) -> None:
    try:
        if len(args) > 1:
            raise ValueError(TRACE_LIST_USAGE)
        try:
            limit = int(args[0]) if args else TRACE_LIST_LIMIT
        except ValueError:
            raise ValueError(TRACE_LIST_USAGE) from None
        if not 1 <= limit <= 100:
            raise ValueError(TRACE_LIST_USAGE)
        result = context.trace_store.list_traces(limit=limit)
    except (OSError, ValueError) as exc:
        print(exc)
        return

    print_trace_list(result)


def _show_trace(context: CommandContext, trace_id: str | None) -> None:
    if trace_id is None:
        trace_id = context.last_trace_id
        if trace_id is None:
            print("No trace available yet.")
            return
    elif not _is_trace_id_prefix(trace_id):
        # A mistyped subcommand is not a trace ID; answer with the usage instead
        # of a misleading "Trace not found".
        print(f"Unknown trace command: {trace_id}")
        handle_help(["trace"])
        return

    try:
        spans = context.trace_store.read(trace_id)
    except (OSError, ValueError) as exc:
        print(exc)
        return

    print_trace(spans)


def _is_trace_id_prefix(value: str) -> bool:
    return bool(value) and all(
        character in "0123456789abcdef" for character in value.lower()
    )


def _delete_traces(context: CommandContext, args: list[str]) -> None:
    try:
        if args and args[0] == "--tail":
            if len(args) != 2:
                raise ValueError(TRACE_DELETE_USAGE)
            result = context.trace_store.delete_oldest(_positive_int(args[1]))
        elif len(args) == 1 and not args[0].startswith("-"):
            result = context.trace_store.delete(args[0])
        else:
            raise ValueError(TRACE_DELETE_USAGE)
    except (OSError, ValueError) as exc:
        print(exc)
        return

    _echo_deletion(context, result)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(TRACE_DELETE_USAGE) from None
    if value < 1:
        raise ValueError(TRACE_DELETE_USAGE)
    return value


def _echo_deletion(context: CommandContext, result: TraceDeleteResult) -> None:
    for warning in result.skipped:
        print(f"warning: {warning}")

    if result.deleted:
        short_ids = ", ".join(trace_id[:8] for trace_id in result.deleted)
        print(f"deleted {len(result.deleted)} trace(s): {short_ids}")
    else:
        print("No trace deleted.")

    if context.last_trace_id in result.deleted:
        context.last_trace_id = None
