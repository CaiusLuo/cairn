from cairn.observability.models import Span
from cairn.observability.tracer import Tracer


class RecordingSink:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def emit(self, span: Span) -> None:
        self.spans.append(span)


def test_tracer_creates_parent_child_relationship() -> None:
    sink = RecordingSink()
    tracer = Tracer(sink)

    root = tracer.start_root_span("agent.turn")
    child = tracer.start_child_span(root, "llm.generate")

    tracer.end_span(child)
    tracer.end_span(root)

    assert child.context.trace_id == root.context.trace_id
    assert child.context.parent_span_id == root.context.span_id

    assert len(sink.spans) == 2
    assert sink.spans[0].name == "llm.generate"
    assert sink.spans[1].name == "agent.turn"
