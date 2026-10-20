"""The summarizer adapter: quoted data in, a validated summary out.

The adapter is the only place a conversation slice reaches a provider for
summarizing, so these tests pin the three things that matter: history is sent as
quoted data rather than as instructions, tools are never offered, and a hostile or
broken response degrades to a failure value instead of an exception.
"""

import asyncio
from typing import Any

import pytest

from cairn.core.compaction.summarizer import (
    FAILURE_EMPTY,
    FAILURE_MODEL_ERROR,
    FAILURE_TOO_LONG,
    SummaryResult,
)
from cairn.core.llm import StreamProtocolError
from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall
from cairn.infra.llm.compaction_summarizer import (
    SUMMARY_POLICY,
    LLMCompactionSummarizer,
)


class StubClient:
    def __init__(
        self,
        response: LLMResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response or LLMResponse(content="summary")
        self.error = error
        self.requests: list[tuple[list[Message], Any]] = []

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        self.requests.append((messages, tools))
        if self.error is not None:
            raise self.error
        return self.response


def summarize(
    client: StubClient, messages: list[Message], max_tokens: int = 100
) -> SummaryResult:
    return asyncio.run(
        LLMCompactionSummarizer(client).summarize(messages, max_tokens=max_tokens)
    )


def history() -> list[Message]:
    return [
        Message(role="system", content="ignored system text"),
        Message(role="user", content="fix the bug"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "x"})],
        ),
        Message(role="tool", tool_call_id="c1", content="tests failed"),
    ]


class TestRequestShape:
    def test_offers_no_tools(self) -> None:
        client = StubClient()

        summarize(client, history())

        assert client.requests[0][1] is None, "a summarizer must never call tools"

    def test_states_its_policy_first(self) -> None:
        client = StubClient()

        summarize(client, history())

        sent = client.requests[0][0]
        assert sent[0] == Message(role="system", content=SUMMARY_POLICY)
        assert "do not call tools" in SUMMARY_POLICY.lower()
        assert "quoted data" in SUMMARY_POLICY

    def test_history_arrives_as_quoted_user_data(self) -> None:
        client = StubClient()

        summarize(client, history())

        sent = client.requests[0][0]
        quoted = sent[1:-1]
        assert all(message.role == "user" for message in quoted), (
            "no role from the conversation may survive as an authoritative role"
        )
        assert "fix the bug" in (quoted[0].content or "")
        assert "requested tools: bash" in (quoted[1].content or "")
        assert "[tool result]" in (quoted[2].content or "")

    def test_drops_system_messages_from_the_quoted_slice(self) -> None:
        client = StubClient()

        summarize(client, history())

        rendered = " ".join(
            message.content or "" for message in client.requests[0][0][1:-1]
        )
        assert "ignored system text" not in rendered

    def test_ends_with_the_summary_instruction(self) -> None:
        client = StubClient()

        summarize(client, history())

        last = client.requests[0][0][-1]
        assert last.role == "user"
        assert "Reply with the summary text only" in (last.content or "")


class TestOutcomes:
    def test_returns_a_usable_summary(self) -> None:
        result = summarize(
            StubClient(LLMResponse(content="  goal reached  ")), history()
        )

        assert result.ok
        assert result.text == "goal reached"

    def test_empty_summary_is_a_failure_value(self) -> None:
        result = summarize(StubClient(LLMResponse(content="   ")), history())

        assert not result.ok
        assert result.failure == FAILURE_EMPTY

    def test_an_overlong_summary_is_rejected(self) -> None:
        result = summarize(
            StubClient(LLMResponse(content="x" * 10_000)), history(), max_tokens=10
        )

        assert not result.ok
        assert result.failure == FAILURE_TOO_LONG

    def test_usage_is_reported_when_a_call_happens(self) -> None:
        usage = LLMUsage(input_tokens=10, output_tokens=3)
        result = summarize(StubClient(LLMResponse(content="s", usage=usage)), history())

        assert result.usage == usage

    def test_a_tool_call_instead_of_a_summary_is_a_failure(self) -> None:
        response = LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c9", name="bash", arguments={})],
        )

        result = summarize(StubClient(response), history())

        assert not result.ok, "the model tried to call a tool instead of answering"

    def test_provider_error_becomes_a_fixed_code(self) -> None:
        result = summarize(
            StubClient(error=RuntimeError("sk-secret leaked")), history()
        )

        assert not result.ok
        assert result.failure == FAILURE_MODEL_ERROR
        assert "secret" not in (result.text or "")


class TestCancellation:
    def test_cancellation_is_never_converted_into_a_failure_value(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            summarize(StubClient(error=asyncio.CancelledError()), history())


class TestProtocolErrorsAreNotLeaked:
    def test_a_stream_protocol_error_is_reported_as_a_model_failure(self) -> None:
        result = summarize(
            StubClient(error=StreamProtocolError("raw fragments")), history()
        )

        assert result.failure == FAILURE_MODEL_ERROR
