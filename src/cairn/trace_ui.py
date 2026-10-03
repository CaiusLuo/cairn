from collections import defaultdict

from rich.console import Console
from rich.text import Text
from rich.tree import Tree

from cairn.observability.models import Span, SpanStatus, TraceListResult, TraceSummary

console = Console()

#: Cap diagnostics so one burst of unreadable traces cannot flood the list.
MAX_DIAGNOSTICS = 5


def _duration(span: Span | TraceSummary) -> str:
    if span.end_time is None:
        return "running"

    seconds = (span.end_time - span.start_time).total_seconds()

    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"

    return f"{seconds:.2f}s"


def _label(span: Span) -> Text:
    icon = (
        "✓"
        if span.status == SpanStatus.OK
        else "✗"
        if span.status == SpanStatus.ERROR
        else "•"
    )

    return Text(f"{icon} {_display_name(span)} [{_duration(span)}]")


def _display_name(span: Span) -> str:
    if span.name == "tool.execute":
        tool = span.attributes.get("tool")
        if isinstance(tool, str) and tool:
            return tool
    return span.name


def print_trace(spans: list[Span]) -> None:
    if not spans:
        console.print("[yellow]Empty trace[/yellow]")
        return

    children: dict[str, list[Span]] = defaultdict(list)
    roots: list[Span] = []

    for span in spans:
        parent_id = span.context.parent_span_id

        if parent_id is None:
            roots.append(span)
        else:
            children[parent_id].append(span)

    for item in children.values():
        item.sort(key=lambda span: span.start_time)

    roots.sort(key=lambda span: span.start_time)

    def add_children(tree: Tree, parent: Span) -> None:
        for child in children.get(parent.context.span_id, []):
            node = tree.add(_label(child))
            add_children(node, child)

    for root in roots:
        tree = Tree(_label(root))
        add_children(tree, root)
        console.print(tree)


def print_trace_list(result: TraceListResult) -> None:
    console.print("Recent traces:")
    if not result.traces:
        console.print("[dim]No traces found.[/dim]")

    for trace in result.traces:
        icon = "✓" if trace.status == SpanStatus.OK else "✗"
        time = trace.start_time.astimezone().strftime("%m-%d %H:%M:%S")
        style = "green" if trace.status == SpanStatus.OK else "red"
        console.print(
            Text(f"{icon} {trace.trace_id[:8]} {time} {_duration(trace)}", style=style)
        )

    for diagnostic in result.diagnostics[:MAX_DIAGNOSTICS]:
        console.print(Text("warning: " + diagnostic))

    hidden = len(result.diagnostics) - MAX_DIAGNOSTICS
    if hidden > 0:
        console.print(Text(f"warning: ... and {hidden} more"))
