import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

import cairn.llm.litellm_client as litellm_module
from cairn.core.models import Message, ToolCall
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
class FakeResponse:
    choices: list[FakeChoice]


def test_to_llm_message_serializes_tool_context() -> None:
    client = LiteLLMClient(model="test-model")
    message = Message(
        role="assistant",
        content=None,
        tool_call_id="parent-call",
        tool_calls=[
            ToolCall(
                id="call-1",
                name="bash",
                arguments={"command": "pwd"},
            )
        ],
    )

    assert client._to_llm_message(message) == {
        "role": "assistant",
        "content": None,
        "tool_call_id": "parent-call",
        "tool_calls": [
            {
                "id": "call-1",
                "name": "function",
                "function": {
                    "name": "bash",
                    "arguments": '{"command": "pwd"}',
                },
            }
        ],
    }


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
                                id="invalid",
                                function=FakeFunction(
                                    name="bash",
                                    arguments="not-json",
                                ),
                            ),
                        ],
                    )
                )
            ]
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
            [Message(role="user", content="hello")],
            tools=tools,
        )
    )

    assert captured == {
        "model": "provider/model",
        "messages": [{"role": "user", "content": "hello"}],
        "api_key": "secret",
        "api_base": "https://example.test/v1",
        "tools": tools,
    }
    assert response.content == "tool requested"
    assert response.tool_calls == [
        ToolCall(id="valid", name="bash", arguments={"command": "pwd"}),
        ToolCall(id="invalid", name="bash", arguments={}),
    ]


def test_generate_handles_response_without_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acompletion(**_kwargs: Any) -> FakeResponse:
        return FakeResponse(
            choices=[
                FakeChoice(
                    message=FakeMessage(content="plain response", tool_calls=None)
                )
            ]
        )

    monkeypatch.setattr(litellm_module, "acompletion", fake_acompletion)

    response = asyncio.run(
        LiteLLMClient(model="test-model").generate(
            [Message(role="user", content="hello")]
        )
    )

    assert response.content == "plain response"
    assert response.tool_calls == []
