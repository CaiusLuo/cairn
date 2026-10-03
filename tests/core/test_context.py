import json
from typing import Any

import pytest

from cairn.core.context import (
    ContextBudget,
    ContextBudgetExceeded,
    ContextBuilder,
    EstimatedTokenCounter,
    TokenCount,
)
from cairn.core.models import Message, ToolCall


class SizeCounter:
    """Exact deterministic test unit: text characters plus schema characters."""

    def __init__(self, notice_tokens: int = 0) -> None:
        self.notice_tokens = notice_tokens
        self.calls: list[tuple[list[Message], list[dict[str, Any]]]] = []

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        self.calls.append((messages.copy(), tools.copy()))
        tokens = sum(
            self.notice_tokens
            if (message.content or "").startswith("Context budget:")
            else len(message.content or "")
            for message in messages
        )
        tokens += sum(len(json.dumps(tool)) for tool in tools)
        return TokenCount(tokens, is_estimate=False)


def user(content: str) -> Message:
    return Message(role="user", content=content)


def assistant(content: str) -> Message:
    return Message(role="assistant", content=content)


def test_full_request_exact_boundary_includes_system_schema_and_reserve() -> None:
    system = [
        Message(role="system", content="system"),
        Message(role="system", content="repo"),
    ]
    history = [user("old"), assistant("answer"), user("current")]
    tools = [{"name": "tool", "parameters": {"type": "object"}}]
    counter = SizeCounter()
    input_tokens = counter.count([*system, *history], tools).tokens
    builder = ContextBuilder(ContextBudget(input_tokens + 7, 7), counter)

    request = builder.build(
        system_messages=system, history=history, current_turn_start=2, tools=tools
    )

    assert request.messages == [*system, *history]
    assert (
        request.tokens_before == request.tokens_after == TokenCount(input_tokens, False)
    )
    assert request.omitted_turns == request.omitted_messages == 0
    assert counter.calls[-1][1] == tools


def test_trimming_keeps_recent_complete_turns_and_does_not_mutate_history() -> None:
    history = [
        user("old"),
        assistant("first"),
        user("recent"),
        assistant("reply"),
        user("now"),
    ]
    snapshot = [message.model_copy(deep=True) for message in history]
    counter = SizeCounter(notice_tokens=4)
    builder = ContextBuilder(ContextBudget(20, 2), counter)

    request = builder.build(
        system_messages=[], history=history, current_turn_start=4, tools=[]
    )

    assert request.omitted_turns == 1
    assert request.omitted_messages == 2
    assert request.messages[0].role == "system"
    assert "omitted 1 earlier complete turn(s) (2 messages)" in (
        request.messages[0].content or ""
    )
    assert request.messages[1:] == history[2:]
    assert request.tokens_before.tokens == 22
    assert request.tokens_after.tokens == 18
    assert history == snapshot
    assert all(
        selected is original
        for selected, original in zip(request.messages[1:], history[2:], strict=True)
    )


def test_omission_notice_is_counted_and_can_require_another_whole_turn() -> None:
    history = [
        user("aaaa"),
        assistant("bbbb"),
        user("cccc"),
        assistant("dddd"),
        user("ee"),
    ]
    builder = ContextBuilder(ContextBudget(13, 1), SizeCounter(notice_tokens=5))

    request = builder.build(
        system_messages=[], history=history, current_turn_start=4, tools=[]
    )

    assert request.omitted_turns == 2
    assert request.omitted_messages == 4
    assert request.messages[1:] == history[4:]
    assert request.tokens_after.tokens == 7


def tool_turn() -> list[Message]:
    return [
        user("tools"),
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="one", name="edit_file", arguments={"new_text": "a"}),
                ToolCall(id="two", name="bash", arguments={"command": "false"}),
            ],
        ),
        Message(
            role="tool", tool_call_id="one", content='{"stdout":"edited","exit_code":0}'
        ),
        Message(
            role="tool",
            tool_call_id="two",
            content='{"type":"TurnAborted","error":"cancelled"}',
        ),
    ]


@pytest.mark.parametrize("keep_old_turn", [False, True])
def test_multiple_tool_calls_and_failed_turn_endings_are_never_split(
    keep_old_turn: bool,
) -> None:
    history = [user("x" * 100), assistant("y" * 100), *tool_turn(), user("current")]
    original = [message.model_copy(deep=True) for message in history]
    counter = SizeCounter(notice_tokens=3)
    limit = 100 if keep_old_turn else 15
    builder = ContextBuilder(ContextBudget(limit, 2), counter)

    request = builder.build(
        system_messages=[], history=history, current_turn_start=6, tools=[]
    )

    assert request.omitted_turns == (1 if keep_old_turn else 2)
    assert request.messages[1:] == history[2 if keep_old_turn else 6 :]
    assert history == original
    if keep_old_turn:
        assert request.messages[2].tool_calls == history[3].tool_calls
        assert [message.tool_call_id for message in request.messages[3:5]] == [
            "one",
            "two",
        ]


def test_current_multi_tool_turn_is_preserved_whole_when_old_turn_is_omitted() -> None:
    history = [user("x" * 100), assistant("y" * 100), *tool_turn()]
    builder = ContextBuilder(ContextBudget(100, 2), SizeCounter(notice_tokens=3))

    request = builder.build(
        system_messages=[], history=history, current_turn_start=2, tools=[]
    )

    assert request.omitted_turns == 1
    assert request.messages[1:] == history[2:]
    assert request.messages[2].tool_calls == history[3].tool_calls
    assert [message.tool_call_id for message in request.messages[3:]] == ["one", "two"]


def test_many_turns_bound_requests_without_deleting_original_conversation() -> None:
    history: list[Message] = []
    builder = ContextBuilder(ContextBudget(40, 5), SizeCounter(notice_tokens=3))
    system = [Message(role="system", content="system")]
    tools = [{"name": "tool"}]

    for _ in range(100):
        current_turn_start = len(history)
        history.append(user("question"))
        request = builder.build(
            system_messages=system,
            history=history,
            current_turn_start=current_turn_start,
            tools=tools,
        )
        assert request.tokens_after.tokens + builder.budget.response_tokens <= 40
        assert request.messages[-1] is history[-1]
        history.append(assistant("answer"))

    assert len(history) == 200
    assert request.omitted_turns == 99
    assert request.omitted_messages == 198
    assert request.tokens_before.tokens > request.tokens_after.tokens


@pytest.mark.parametrize("required_part", ["system", "schema", "input", "tools"])
def test_required_context_oversize_has_clear_error_and_preserves_facts(
    required_part: str,
) -> None:
    system = (
        [Message(role="system", content="s" * 30)] if required_part == "system" else []
    )
    tools = [{"description": "s" * 30}] if required_part == "schema" else []
    history = (
        tool_turn()
        if required_part == "tools"
        else [user("i" * 30 if required_part == "input" else "now")]
    )
    original = [message.model_copy(deep=True) for message in history]
    builder = ContextBuilder(ContextBudget(20, 5), SizeCounter())

    with pytest.raises(ContextBudgetExceeded) as raised:
        builder.build(
            system_messages=system, history=history, current_turn_start=0, tools=tools
        )

    error = raised.value
    assert error.input_tokens + error.response_tokens > error.max_tokens
    assert error.response_tokens == 5
    assert error.max_tokens == 20
    assert error.is_estimate is False
    assert "exact input" in str(error)
    assert "current turn and tool results cannot be trimmed" in str(error)
    assert history == original


def test_required_context_with_omission_notice_can_still_exceed_budget() -> None:
    history = [user("old"), assistant("answer"), user("now")]
    builder = ContextBuilder(ContextBudget(12, 2), SizeCounter(notice_tokens=8))

    with pytest.raises(ContextBudgetExceeded) as raised:
        builder.build(
            system_messages=[], history=history, current_turn_start=2, tools=[]
        )

    assert raised.value.input_tokens == 11
    assert raised.value.response_tokens == 2


def test_offline_counter_marks_estimates_and_counts_utf8_arguments_and_schemas() -> (
    None
):
    counter = EstimatedTokenCounter()
    plain = counter.count([user("abc")], [])
    unicode_text = counter.count([user("汉字文")], [])
    tool_messages = tool_turn()
    tool_schema = [{"name": "edit_file", "parameters": {"description": "汉字文"}}]

    assert plain.is_estimate is True
    assert unicode_text.tokens > plain.tokens
    assert (
        counter.count(tool_messages, tool_schema).tokens
        > counter.count(tool_messages, []).tokens
    )
    longer_arguments = [message.model_copy(deep=True) for message in tool_messages]
    longer_arguments[1].tool_calls[0].arguments["new_text"] = "z" * 100
    assert (
        counter.count(longer_arguments, []).tokens
        > counter.count(tool_messages, []).tokens
    )
    builder = ContextBuilder(ContextBudget(2, 1))
    with pytest.raises(ContextBudgetExceeded, match="estimated input"):
        builder.build(
            system_messages=[], history=[user("a")], current_turn_start=0, tools=[]
        )


@pytest.mark.parametrize(
    "max_tokens,response_tokens",
    [(0, 1), (-1, 1), (10, -1), (10, 0), (10, 10), (10, 11), (True, 1), (10, False)],
)
def test_invalid_budgets_are_rejected(max_tokens: int, response_tokens: int) -> None:
    with pytest.raises(ValueError):
        ContextBudget(max_tokens, response_tokens)


@pytest.mark.parametrize("tokens", [-1, True, 1.5])
def test_invalid_token_counts_are_rejected(tokens: int) -> None:
    with pytest.raises(ValueError):
        TokenCount(tokens, False)


@pytest.mark.parametrize(
    "history,current_turn_start",
    [
        ([], 0),
        ([user("current")], -1),
        ([user("current")], 1),
        ([assistant("old"), user("current")], 1),
        ([user("current"), user("new")], 0),
        ([user("old"), assistant("answer")], 1),
        (
            [
                user("now"),
                Message(role="tool", tool_call_id="missing", content="result"),
            ],
            0,
        ),
        (tool_turn()[:-1], 0),
        ([*tool_turn()[:-1], user("current")], 3),
        (
            [
                user("now"),
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(id="same", name="tool", arguments={}),
                        ToolCall(id="same", name="tool", arguments={}),
                    ],
                ),
            ],
            0,
        ),
    ],
)
def test_invalid_boundaries_and_tool_groups_fail_safely(
    history: list[Message], current_turn_start: int
) -> None:
    with pytest.raises(ValueError):
        ContextBuilder(counter=SizeCounter()).build(
            system_messages=[],
            history=history,
            current_turn_start=current_turn_start,
            tools=[],
        )
