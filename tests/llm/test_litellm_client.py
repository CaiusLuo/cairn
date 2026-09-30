import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

import cairn.llm.litellm_client as litellm_module
from cairn.core.models import LLMUsage, Message, ToolCall
from cairn.llm.litellm_client import LiteLLMClient


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


def test_generate_forwards_request_and_parses_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
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

    assert captured == {
        "model": "provider/model",
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
