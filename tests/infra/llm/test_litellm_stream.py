import asyncio
from datetime import datetime
from typing import Any

import httpx
import litellm
import pytest
from litellm.exceptions import APIError, MidStreamFallbackError, RateLimitError
from litellm.litellm_core_utils.litellm_logging import Logging
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from openai.types.chat import ChatCompletionChunk

import cairn.infra.llm.litellm_client as client_module
from cairn.core.budget import RunBudget
from cairn.core.llm import StreamProtocolError
from cairn.core.models import LLMUsage, Message
from cairn.infra.llm.errors import is_fallback_error
from cairn.infra.llm.litellm_client import LiteLLMClient
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from cairn.runtime.response_assembly import ResponseAssembler
from tests.support.runtime import RecordingTool
from tests.support.streaming import ProviderStream, StreamBarrier, provider_chunk


@pytest.fixture(autouse=True)
def offline_sdk_token_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # Real SDK EOF synthesis may count tokens. Keep that test deterministic and
    # independent of a tokenizer cache/download; Cairn must ignore its 999 anyway.
    monkeypatch.setattr(litellm, "token_counter", lambda **kwargs: 999)


async def collect(client: LiteLLMClient) -> ResponseAssembler:
    assembler = ResponseAssembler()
    async for chunk in client.stream_generate([Message(role="user", content="hello")]):
        assembler.add(chunk)
    return assembler


def install(monkeypatch: pytest.MonkeyPatch, stream: object) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> object:
        requests.append(kwargs)
        return stream

    monkeypatch.setattr(client_module, "acompletion", completion)
    return requests


def test_stream_options_serialization_unicode_and_partial_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = ProviderStream(
        provider_chunk("你"),
        provider_chunk("🌳"),
        provider_chunk(finish="stop"),
        {"choices": [], "usage": {"prompt_tokens": 17}},
    )
    requests = install(monkeypatch, stream)
    response = asyncio.run(
        collect(
            LiteLLMClient(
                "openai/test",
                api_key="secret",
                api_base="https://example.test",
                max_output_tokens=42,
            )
        )
    ).finish()
    assert response.content == "你🌳"
    assert response.usage == LLMUsage(input_tokens=17)
    assert stream.closed
    assert len(requests) == 1
    assert requests[0] == {
        "model": "openai/test",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": None,
        "api_key": "secret",
        "api_base": "https://example.test",
        "max_tokens": 42,
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 0,
        "max_retries": 0,
    }


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"choices": []},
        {"choices": [{"index": 1, "delta": {}}]},
        {"choices": [{"index": 0, "delta": {}}, {"index": 1, "delta": {}}]},
        {"choices": [{"index": 0, "delta": {"role": "user"}}]},
        provider_chunk(tool_calls=[{"index": 0, "type": "bad", "function": {}}]),
        provider_chunk(tool_calls=[{"index": 0}]),
        provider_chunk(tool_calls=[{"index": 0, "function": {"arguments": {}}}]),
        {"choices": [{"index": 0, "delta": {"function_call": {"name": "secret"}}}]},
        provider_chunk(usage={"prompt_tokens": True}),
    ],
)
def test_invalid_provider_shape_closes_without_raw_diagnostics(
    monkeypatch: pytest.MonkeyPatch, raw: object
) -> None:
    stream = ProviderStream(raw)
    requests = install(monkeypatch, stream)
    with pytest.raises(StreamProtocolError) as error:
        asyncio.run(collect(LiteLLMClient("test")))
    assert "secret" not in str(error.value)
    assert stream.closed
    assert len(requests) == 1


def sdk_stream(*chunks: dict[str, Any]) -> tuple[CustomStreamWrapper, ProviderStream]:
    # Real SDK objects/normalization, no provider/network call. This pins the
    # synthetic-finish/usage behavior of the installed LiteLLM release.
    source = ProviderStream(
        *[
            ChatCompletionChunk(
                id="chat-test",
                object="chat.completion.chunk",
                created=1,
                model="test",
                **chunk,
            )
            for chunk in chunks
        ]
    )
    logging = Logging(
        model="test",
        messages=[],
        stream=True,
        call_type="acompletion",
        start_time=datetime.now(),
        litellm_call_id="offline",
        function_id="offline",
    )
    wrapper = CustomStreamWrapper(
        source,
        model="test",
        custom_llm_provider="openai",
        logging_obj=logging,
        stream_options={"include_usage": True},
    )
    return wrapper, source


@pytest.mark.parametrize("reported", [False, True])
def test_real_sdk_synthetic_usage_is_ignored(
    monkeypatch: pytest.MonkeyPatch, reported: bool
) -> None:
    chunks = [provider_chunk("answer"), provider_chunk(finish="stop")]
    if reported:
        chunks.append(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                },
            }
        )
    wrapper, source = sdk_stream(*chunks)
    install(monkeypatch, wrapper)
    response = asyncio.run(collect(LiteLLMClient("test"))).finish()
    assert response.content == "answer"
    assert response.usage == (
        LLMUsage(input_tokens=7, output_tokens=3) if reported else None
    )
    assert source.closed


def test_real_sdk_synthetic_finish_does_not_accept_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper, source = sdk_stream(provider_chunk("unfinished"))
    install(monkeypatch, wrapper)
    assembler = asyncio.run(collect(LiteLLMClient("test")))
    with pytest.raises(StreamProtocolError, match="without provider completion"):
        assembler.finish()
    assert source.closed


def test_real_sdk_interleaved_tool_fragments_are_observed_before_sdk_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def call(
        index: int, arguments: str, id: str | None = None, name: str | None = None
    ) -> dict[str, Any]:
        return {
            "index": index,
            "id": id,
            "type": "function" if id else None,
            "function": {"name": name, "arguments": arguments},
        }

    wrapper, source = sdk_stream(
        provider_chunk(tool_calls=[call(0, '{"path":"', "first", "read_file")]),
        provider_chunk(tool_calls=[call(1, '{"command":', "second", "bash")]),
        provider_chunk(tool_calls=[call(0, '文档"}')]),
        provider_chunk(tool_calls=[call(1, '"pwd"}')]),
        provider_chunk(finish="tool_calls"),
    )
    install(monkeypatch, wrapper)
    response = asyncio.run(collect(LiteLLMClient("test"))).finish()
    assert [item.id for item in response.tool_calls] == ["first", "second"]
    assert [item.arguments for item in response.tool_calls] == [
        {"path": "文档"},
        {"command": "pwd"},
    ]
    assert response.usage is None and source.closed


def test_partial_error_omits_generated_content_and_preserves_shared_quota_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AccountQuotaError(RuntimeError):
        code = "insufficient_quota"

    quota = AccountQuotaError("provider account quota")
    original = RateLimitError(
        message='raw {"secret":"argument"}', model="test", llm_provider="openai"
    )
    original.__context__ = quota
    wrapper_error = MidStreamFallbackError(
        message="failed",
        model="test",
        llm_provider="openai",
        original_exception=original,
        generated_content="raw-generated-secret",
    )
    stream = ProviderStream(provider_chunk("partial"), wrapper_error)
    install(monkeypatch, stream)
    with pytest.raises(RateLimitError) as raised:
        asyncio.run(collect(LiteLLMClient("test")))
    assert raised.value is original and original.__context__ is quota
    assert not is_fallback_error(original)
    assert "raw" not in str(original) and "argument" not in repr(original)
    assert stream.closed


def test_partial_message_only_free_tier_error_keeps_narrow_fallback_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = APIError(
        status_code=403,
        message="Free quota exhausted; use free tier only. raw-secret",
        model="test",
        llm_provider="openai",
    )
    install(monkeypatch, ProviderStream(provider_chunk("partial"), original))
    with pytest.raises(APIError) as raised:
        asyncio.run(collect(LiteLLMClient("test")))
    assert raised.value is original and is_fallback_error(original)
    assert "raw-secret" not in str(original)


def test_real_sdk_cleanup_failure_is_detected_even_if_sdk_swallows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper, source = sdk_stream(
        provider_chunk("answer"), provider_chunk(finish="stop")
    )

    async def fail_close() -> None:
        raise RuntimeError("secret close diagnostic")

    monkeypatch.setattr(source, "aclose", fail_close)
    install(monkeypatch, wrapper)
    with pytest.raises(StreamProtocolError, match="cleanup failed") as raised:
        asyncio.run(collect(LiteLLMClient("test")))
    assert "secret" not in str(raised.value)


def test_sdk_remapping_cannot_hide_cairn_protocol_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper, source = sdk_stream(
        provider_chunk(
            tool_calls=[
                {
                    "index": -1,
                    "id": "call",
                    "function": {"name": "bash", "arguments": "{}"},
                }
            ]
        )
    )
    install(monkeypatch, wrapper)
    with pytest.raises(StreamProtocolError, match="fragment identity"):
        asyncio.run(collect(LiteLLMClient("test")))
    assert source.closed


@pytest.mark.parametrize("failure_type", [httpx.ReadError, httpx.RemoteProtocolError])
def test_sdk_cannot_hide_transport_failure_after_finish(
    monkeypatch: pytest.MonkeyPatch, failure_type: type[httpx.RequestError]
) -> None:
    wrapper, source = sdk_stream(
        provider_chunk("answer"), provider_chunk(finish="stop")
    )
    source.items = iter(
        [*source.items, failure_type("secret late transport diagnostic")]
    )
    install(monkeypatch, wrapper)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(collect(LiteLLMClient("test"))).finish()
    assert "secret" not in str(error.value)
    assert source.closed


def test_hidden_sdk_failure_cannot_dispatch_completed_tool_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper, source = sdk_stream(
        provider_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call",
                    "type": "function",
                    "function": {"name": "record", "arguments": '{"value":1}'},
                }
            ]
        ),
        provider_chunk(finish="tool_calls"),
    )
    source.items = iter([*source.items, httpx.ReadError("late transport failure")])
    install(monkeypatch, wrapper)
    registry = ToolRegistry()
    tool = RecordingTool()
    registry.register_tool(tool)
    agent = Agent(llm=LiteLLMClient("test"), tools=registry)
    with pytest.raises(RuntimeError, match="LLM provider streaming failed"):
        asyncio.run(run_turn(agent, "hello", budget=RunBudget(max_steps=1)))
    assert agent.state.messages == []
    assert tool.calls == [] and source.closed


def test_midstream_sdk_wrapper_preserves_original_eligibility_and_redacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RateLimitError(message="rate limit", model="test", llm_provider="openai")
    error = MidStreamFallbackError(
        message="rate limit",
        model="test",
        llm_provider="openai",
        original_exception=original,
        generated_content="partial",
    )
    stream = ProviderStream(provider_chunk("partial"), error)
    requests = install(monkeypatch, stream)
    with pytest.raises(RateLimitError) as raised:
        asyncio.run(collect(LiteLLMClient("test")))
    assert raised.value is original
    assert stream.closed and len(requests) == 1
    secret_stream = ProviderStream(RuntimeError("credential TOPSECRET"))
    install(monkeypatch, secret_stream)
    with pytest.raises(RuntimeError, match="details were omitted") as sanitized:
        asyncio.run(collect(LiteLLMClient("test", api_key="TOPSECRET")))
    assert "TOPSECRET" not in str(sanitized.value)
    assert secret_stream.closed


def test_cancellation_and_repeated_cleanup_settle_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        blocked = StreamBarrier(asyncio.Event(), asyncio.Event())
        closing = StreamBarrier(asyncio.Event(), asyncio.Event())
        stream = ProviderStream(provider_chunk("partial"), blocked)
        stream.close_barrier = closing
        install(monkeypatch, stream)
        task = asyncio.create_task(collect(LiteLLMClient("test")))
        await blocked.entered.wait()
        task.cancel()
        await closing.entered.wait()
        task.cancel()
        closing.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(scenario())


def test_cancelled_acquisition_closes_late_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        stream = ProviderStream()

        async def completion(**kwargs: Any) -> ProviderStream:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                return stream
            raise AssertionError("The acquisition must be cancelled.")

        monkeypatch.setattr(client_module, "acompletion", completion)
        task = asyncio.create_task(collect(LiteLLMClient("test")))
        await entered.wait()
        task.cancel()
        await cancelled.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        assert not [
            owned
            for owned in asyncio.all_tasks()
            if owned is not asyncio.current_task() and not owned.done()
        ]

    asyncio.run(scenario())


def test_late_cancellation_is_primary_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        source = ProviderStream(provider_chunk("answer"), provider_chunk(finish="stop"))

        async def close() -> None:
            entered.set()
            await release.wait()
            raise RuntimeError("secret cleanup diagnostic")

        monkeypatch.setattr(source, "aclose", close)
        install(monkeypatch, source)
        task = asyncio.create_task(collect(LiteLLMClient("test")))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        assert "secret" not in repr(getattr(error.value, "__notes__", []))
        assert not [
            owned
            for owned in asyncio.all_tasks()
            if owned is not asyncio.current_task() and not owned.done()
        ]

    asyncio.run(scenario())
