from collections import defaultdict

from rich.console import Console
from rich.text import Text
from rich.tree import Tree

from cairn.observability.models import Span, SpanStatus

console = Console()


def _duration(span: Span) -> str:
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

    return Text(f"{icon} {span.name} [{_duration(span)}]")


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
