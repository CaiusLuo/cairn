import pytest

from cairn.core.llm import LLMStreamChunk, StreamProtocolError, ToolCallDelta
from cairn.core.models import LLMUsage
from cairn.runtime.response_assembly import ResponseAssembler, StreamLimits


def assemble(*chunks: LLMStreamChunk) -> ResponseAssembler:
    assembler = ResponseAssembler()
    for chunk in chunks:
        assembler.add(chunk)
    return assembler


def test_ordered_unicode_text_and_exact_partial_usage() -> None:
    usage = LLMUsage(input_tokens=17)
    assembler = assemble(
        LLMStreamChunk(text="你好 "),
        LLMStreamChunk(text="🌳"),
        LLMStreamChunk(finish_reason="stop"),
        LLMStreamChunk(usage=usage),
    )
    response = assembler.finish()
    assert response.content == "你好 🌳"
    assert response.usage == LLMUsage(input_tokens=17)
    usage.input_tokens = 999
    assert response.usage.input_tokens == 17
    assert assemble(LLMStreamChunk(finish_reason="stop")).finish().usage is None


def test_interleaved_calls_fragmented_identity_and_arguments() -> None:
    assembler = assemble(
        LLMStreamChunk(tool_calls=(ToolCallDelta(0, "call_", "read_"),)),
        LLMStreamChunk(tool_calls=(ToolCallDelta(0, "a", "file", '{"path":"'),)),
        LLMStreamChunk(tool_calls=(ToolCallDelta(1, "call_b", "bash", '{"command":'),)),
        LLMStreamChunk(tool_calls=(ToolCallDelta(0, arguments='文档"}'),)),
        LLMStreamChunk(tool_calls=(ToolCallDelta(1, arguments='"pwd"}'),)),
    )
    # Both calls are parseable; the response group is still provisional.
    with pytest.raises(StreamProtocolError, match="without provider completion"):
        assembler.finish()
    assembler.add(LLMStreamChunk(finish_reason="tool_calls"))
    response = assembler.finish()
    assert [call.id for call in response.tool_calls] == ["call_a", "call_b"]
    assert [call.name for call in response.tool_calls] == ["read_file", "bash"]
    assert [call.arguments for call in response.tool_calls] == [
        {"path": "文档"},
        {"command": "pwd"},
    ]


@pytest.mark.parametrize(
    "reason", ["length", "content_filter", "function_call", "unknown"]
)
def test_unsuccessful_finish_is_rejected(reason: str) -> None:
    with pytest.raises(StreamProtocolError, match="finish"):
        assemble(LLMStreamChunk(text="partial", finish_reason=reason))


@pytest.mark.parametrize(
    "arguments", ["", "{", "[]", "null", '{"x":NaN}', '{"x":1e309}', '{"x":1,"x":2}']
)
def test_incomplete_or_invalid_tool_arguments(arguments: str) -> None:
    with pytest.raises(StreamProtocolError, match="JSON object"):
        assemble(
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "call", "bash", arguments),)),
            LLMStreamChunk(finish_reason="tool_calls"),
        ).finish()


@pytest.mark.parametrize(
    "chunks",
    [
        (LLMStreamChunk(text="partial"),),
        (LLMStreamChunk(finish_reason="tool_calls"),),
        (
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "x", "bash", "{}"),)),
            LLMStreamChunk(finish_reason="stop"),
        ),
        (LLMStreamChunk(tool_calls=(ToolCallDelta(1, "x", "bash", "{}"),)),),
        (
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "x", "bash", "{}"),)),
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "y"),)),
        ),
        (
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "x", "bash", "{}"),)),
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, name="other"),)),
        ),
        (
            LLMStreamChunk(
                tool_calls=(
                    ToolCallDelta(0, "x", "bash", "{}"),
                    ToolCallDelta(1, "x", "bash", "{}"),
                )
            ),
            LLMStreamChunk(finish_reason="tool_calls"),
        ),
        (
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "", "bash", "{}"),)),
            LLMStreamChunk(finish_reason="tool_calls"),
        ),
        (
            LLMStreamChunk(tool_calls=(ToolCallDelta(0, "x", "", "{}"),)),
            LLMStreamChunk(finish_reason="tool_calls"),
        ),
        (LLMStreamChunk(tool_calls=(ToolCallDelta(0), ToolCallDelta(0))),),
        (LLMStreamChunk(finish_reason="stop"), LLMStreamChunk(text="late")),
        (LLMStreamChunk(finish_reason="stop"), LLMStreamChunk(finish_reason="stop")),
        (
            LLMStreamChunk(usage=LLMUsage(input_tokens=2)),
            LLMStreamChunk(usage=LLMUsage(input_tokens=1)),
            LLMStreamChunk(finish_reason="stop"),
        ),
        (LLMStreamChunk(text="\ud800"),),
    ],
)
def test_invalid_groups_and_sequences_fail_closed(
    chunks: tuple[LLMStreamChunk, ...],
) -> None:
    with pytest.raises(StreamProtocolError):
        assemble(*chunks).finish()


@pytest.mark.parametrize(
    ("limits", "chunks"),
    [
        (StreamLimits(text_bytes=3), (LLMStreamChunk(text="你好"),)),
        (
            StreamLimits(argument_bytes=3),
            (LLMStreamChunk(tool_calls=(ToolCallDelta(0, "x", "bash", '{"x":0}'),)),),
        ),
        (
            StreamLimits(tool_calls=1),
            (LLMStreamChunk(tool_calls=(ToolCallDelta(0), ToolCallDelta(1))),),
        ),
        (
            StreamLimits(chunks=1),
            (LLMStreamChunk(text="a"), LLMStreamChunk(finish_reason="stop")),
        ),
        (StreamLimits(), (LLMStreamChunk(tool_calls=(ToolCallDelta(0, "x" * 257),)),)),
        (
            StreamLimits(),
            (LLMStreamChunk(tool_calls=(ToolCallDelta(0, name="x" * 129),)),),
        ),
    ],
)
def test_cumulative_limits(
    limits: StreamLimits, chunks: tuple[LLMStreamChunk, ...]
) -> None:
    assembler = ResponseAssembler(limits)
    with pytest.raises(StreamProtocolError, match="limit"):
        for chunk in chunks:
            assembler.add(chunk)


def test_limits_must_be_positive() -> None:
    with pytest.raises(ValueError):
        StreamLimits(chunks=0)


def test_discard_blocks_reuse_and_keeps_returned_response_detached() -> None:
    assembler = assemble(
        LLMStreamChunk(text="accepted"),
        LLMStreamChunk(finish_reason="stop"),
        LLMStreamChunk(usage=LLMUsage(output_tokens=3)),
    )
    response = assembler.finish()
    assembler.discard()
    assert response.content == "accepted"
    assert response.usage == LLMUsage(output_tokens=3)
    with pytest.raises(StreamProtocolError, match="discarded"):
        assembler.finish()
    with pytest.raises(StreamProtocolError, match="discarded"):
        assembler.add(LLMStreamChunk(finish_reason="stop"))
