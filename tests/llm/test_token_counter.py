from typing import Any

import pytest

import cairn.llm.token_counter as token_counter_module
from cairn.core.context import EstimatedTokenCounter, TokenCount
from cairn.core.models import Message, ToolCall
from cairn.llm.litellm_client import to_llm_message
from cairn.llm.token_counter import LiteLLMTokenCounter


def test_real_tokenizer_counts_are_marked_exact_and_grow_with_content() -> None:
    counter = LiteLLMTokenCounter("gpt-4o")
    short = counter.count([Message(role="user", content="hi")], [])
    long = counter.count([Message(role="user", content="hi " * 200)], [])
    tools = [{"type": "function", "function": {"name": "bash", "description": "run"}}]
    with_tools = counter.count([Message(role="user", content="hi")], tools)

    assert short.is_estimate is False
    assert short.tokens > 0
    assert short.tokens < long.tokens
    assert short.tokens < with_tools.tokens


def test_counts_exactly_the_payload_that_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_token_counter(
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        captured.update(model=model, messages=messages, tools=tools)
        return 42

    monkeypatch.setattr(token_counter_module, "token_counter", fake_token_counter)
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="run it"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "ls"})],
        ),
        Message(role="tool", tool_call_id="c1", content='{"exit_code":0}'),
    ]
    tools = [{"type": "function", "function": {"name": "bash"}}]

    count = LiteLLMTokenCounter("provider/model").count(messages, tools)

    assert count == TokenCount(42, is_estimate=False)
    assert captured["model"] == "provider/model"
    assert captured["messages"] == [to_llm_message(message) for message in messages]
    assert captured["tools"] == tools


def test_counted_payload_matches_the_adapter_serialization() -> None:
    message = Message(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCall(id="c1", name="edit_file", arguments={"new_text": "汉字"}),
        ],
    )

    assert to_llm_message(message) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "arguments": '{"new_text": "汉字"}',
                },
            }
        ],
    }


@pytest.mark.parametrize("tools", [[], None])
def test_empty_tool_lists_are_not_forwarded_to_litellm(
    monkeypatch: pytest.MonkeyPatch, tools: list[dict[str, Any]] | None
) -> None:
    captured: dict[str, Any] = {}

    def fake_token_counter(
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        captured["tools"] = tools
        return 1

    monkeypatch.setattr(token_counter_module, "token_counter", fake_token_counter)

    count = LiteLLMTokenCounter("model").count(
        [Message(role="user", content="x")], list(tools or [])
    )

    assert count == TokenCount(1, is_estimate=False)
    assert captured["tools"] is None


def test_falls_back_to_a_labelled_estimate_when_litellm_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_token_counter(**kwargs: Any) -> int:
        raise ValueError(f"no tokenizer for {kwargs['model']}")

    monkeypatch.setattr(token_counter_module, "token_counter", failing_token_counter)
    messages = [Message(role="user", content="汉字" * 10)]

    count = LiteLLMTokenCounter("unlisted/model").count(messages, [])

    assert count == EstimatedTokenCounter().count(messages, [])
    assert count.is_estimate is True


@pytest.mark.parametrize("returned", ["42", -1, 1.5, None])
def test_invalid_litellm_counts_fall_back_to_a_labelled_estimate(
    monkeypatch: pytest.MonkeyPatch, returned: object
) -> None:
    def fake_token_counter(**kwargs: Any) -> object:
        return returned

    monkeypatch.setattr(token_counter_module, "token_counter", fake_token_counter)
    messages = [Message(role="user", content="x")]

    count = LiteLLMTokenCounter("model").count(messages, [])

    assert count == EstimatedTokenCounter().count(messages, [])
    assert count.is_estimate is True
