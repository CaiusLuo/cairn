import asyncio
from dataclasses import dataclass
from typing import Any

import litellm
import pytest

import cairn.infra.llm.litellm_client as litellm_module
from cairn.core.llm import StreamingLLMClient, StreamProtocolError
from cairn.core.models import LLMUsage, Message, ToolCall
from cairn.infra.llm.litellm_client import LiteLLMClient


@pytest.mark.parametrize(
    ("model", "supported"),
    [
        ("openai/local-model", True),
        ("azure/deployment", True),
        ("openai/known-chat", True),
        ("openai/known-responses", False),
        ("azure/known-responses", False),
        ("openai/text-davinci-003", False),
        ("openai/ft:babbage-002:test", False),
        ("azure/ft:davinci-002:test", False),
        ("openai/", False),
        ("azure/responses/deployment", False),
    ],
)
def test_stream_capability_is_local_and_explicit(
    monkeypatch: pytest.MonkeyPatch, model: str, supported: bool
) -> None:
    monkeypatch.delenv("USE_LITELLM_PROXY", raising=False)
    monkeypatch.setattr(litellm, "use_litellm_proxy", False)
    monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", False)
    monkeypatch.setattr(
        litellm,
        "model_cost",
        {
            "openai/known-chat": {"mode": "chat"},
            "known-responses": {"mode": "responses"},
            "text-davinci-003": {"mode": "completion"},
        },
    )
    calls: list[dict[str, Any]] = []

    async def forbidden(**kwargs: Any) -> None:
        calls.append(kwargs)
        raise AssertionError("Capability selection must not issue a provider request")

    monkeypatch.setattr(litellm_module, "acompletion", forbidden)
    client = LiteLLMClient(model, api_key="credential", api_base="https://example.test")
    assert isinstance(client, StreamingLLMClient)
    assert client.supports_streaming is supported
    if not supported:
        with pytest.raises(StreamProtocolError, match="unsupported"):
            client.stream_generate([])
    assert calls == []


@pytest.mark.parametrize("override", ["sdk-proxy", "env-proxy", "responses"])
def test_sdk_routing_overrides_decline_streaming_before_request(
    monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    monkeypatch.delenv("USE_LITELLM_PROXY", raising=False)
    monkeypatch.setattr(litellm, "use_litellm_proxy", override == "sdk-proxy")
    monkeypatch.setattr(
        litellm,
        "route_all_chat_openai_to_responses",
        override == "responses",
    )
    if override == "env-proxy":
        monkeypatch.setenv("USE_LITELLM_PROXY", "true")
    client = LiteLLMClient("openai/local-model")
    assert client.supports_streaming is False
    with pytest.raises(StreamProtocolError):
        client.stream_generate([])


@dataclass
class FakeFunction:
    name: str
    arguments: str | None


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction


@dataclass
class FakeMessage:
    content: str | None
    tool_calls: list[FakeToolCall] | None


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeUsage:
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass
class FakeResponse:
    choices: list[FakeChoice]
    usage: FakeUsage | None = None


@pytest.mark.parametrize("max_output_tokens", [None, 256])
def test_generate_forwards_request_and_parses_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
    max_output_tokens: int | None,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(
            choices=[
                FakeChoice(
                    message=FakeMessage(
                        content="tool requested",
                        tool_calls=[
                            FakeToolCall(
                                id="valid",
                                function=FakeFunction(
                                    name="bash",
                                    arguments='{"command": "pwd"}',
                                ),
                            ),
                            FakeToolCall(
                                id="empty",
                                function=FakeFunction(
                                    name="bash",
                                    arguments="",
                                ),
                            ),
                            FakeToolCall(
                                id="none",
                                function=FakeFunction(
                                    name="bash",
                                    arguments=None,
                                ),
                            ),
                        ],
                    )
                )
            ],
            usage=FakeUsage(prompt_tokens=18, completion_tokens=4),
        )

    monkeypatch.setattr(litellm_module, "acompletion", fake_acompletion)
    tools: list[dict[str, Any]] = [{"type": "function"}]
    client = LiteLLMClient(
        model="provider/model",
        api_key="secret",
        api_base="https://example.test/v1",
        max_output_tokens=max_output_tokens,
    )

    response = asyncio.run(
        client.generate(
            [
                Message(role="user", content="hello"),
                Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="bash",
                            arguments={"command": "pwd"},
                        )
                    ],
                ),
                Message(role="tool", tool_call_id="call-1", content="pwd output"),
            ],
            tools=tools,
        )
    )

    if max_output_tokens is None:
        assert "max_tokens" not in captured
    else:
        assert captured.pop("max_tokens") == max_output_tokens
    assert captured == {
        "model": "provider/model",
        "num_retries": 0,
        "max_retries": 0,
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command": "pwd"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "pwd output",
            },
        ],
        "api_key": "secret",
        "api_base": "https://example.test/v1",
        "tools": tools,
    }
    assert response.content == "tool requested"
    assert response.tool_calls == [
        ToolCall(id="valid", name="bash", arguments={"command": "pwd"}),
        ToolCall(id="empty", name="bash", arguments={}),
        ToolCall(id="none", name="bash", arguments={}),
    ]
    assert response.usage == LLMUsage(input_tokens=18, output_tokens=4)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ("not-json", "Invalid JSON arguments"),
        ("[]", "must be a JSON object"),
    ],
)
def test_generate_rejects_invalid_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
    arguments: str,
    message: str,
) -> None:
    async def fake_acompletion(**_kwargs: Any) -> FakeResponse:
        return FakeResponse(
            choices=[
                FakeChoice(
                    message=FakeMessage(
                        content=None,
                        tool_calls=[
                            FakeToolCall(
                                id="bad-call",
                                function=FakeFunction(
                                    name="bash",
                                    arguments=arguments,
                                ),
                            )
                        ],
                    )
                )
            ]
        )

    monkeypatch.setattr(litellm_module, "acompletion", fake_acompletion)

    with pytest.raises(ValueError, match=message) as error:
        asyncio.run(
            LiteLLMClient(model="test-model").generate(
                [Message(role="user", content="hello")]
            )
        )

    assert "bash" in str(error.value)
    assert "bad-call" in str(error.value)


@pytest.mark.parametrize(
    ("usage", "expected_usage"),
    [
        (None, None),
        (
            FakeUsage(prompt_tokens=12, completion_tokens=None),
            LLMUsage(input_tokens=12),
        ),
    ],
)
def test_generate_handles_plain_response_and_partial_usage(
    monkeypatch: pytest.MonkeyPatch,
    usage: FakeUsage | None,
    expected_usage: LLMUsage | None,
) -> None:
    async def fake_acompletion(**_kwargs: Any) -> FakeResponse:
        return FakeResponse(
            choices=[
                FakeChoice(
                    message=FakeMessage(content="plain response", tool_calls=None)
                )
            ],
            usage=usage,
        )

    monkeypatch.setattr(litellm_module, "acompletion", fake_acompletion)

    response = asyncio.run(
        LiteLLMClient(model="test-model").generate(
            [Message(role="user", content="hello")]
        )
    )

    assert response.content == "plain response"
    assert response.tool_calls == []
    assert response.usage == expected_usage
