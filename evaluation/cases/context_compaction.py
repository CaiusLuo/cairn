"""Fixed compaction cases: what a long turn must preserve.

Each case names one way a conversation outgrows its budget, plus the facts that
must survive whatever the compaction policy does. The cases are deterministic and
offline: a scripted client produces the tool output and the summary, so a failure
here is a defect in Cairn, never a model's mood.

A case deliberately does not pin token counts or message indices. Those are
consequences of the policy, and pinning them would turn every tuning change into
a test rewrite. What is pinned is the behaviour a user would notice: the turn
finishes, executed facts are not lost or invented, and the current instruction is
still sent verbatim.
"""

from dataclasses import dataclass, field
from typing import Any

from cairn.core.models import LLMResponse, Message, ToolCall


@dataclass(frozen=True, slots=True)
class CompactionCase:
    """One scripted scenario plus the facts that must survive it."""

    name: str
    description: str
    responses: tuple[LLMResponse, ...]
    tool_output: str
    #: Text the user typed in the current turn; it must reach the model verbatim.
    current_instruction: str
    #: Text a preceding turn contributed; it may be summarized away.
    older_history: tuple[str, ...] = ()
    #: Budget in tokens, chosen so the raw request exceeds it.
    budget_tokens: int = 1_000
    system_prompt: str = "sys"
    #: Tool calls the scripted model issues per response, in order.
    expected_tool_runs: int = 1


def tool_call_response(call_id: str, name: str = "big") -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments={"value": 1})],
    )


def finish_response() -> LLMResponse:
    return LLMResponse(content="finished")


def single_oversized_tool_output() -> CompactionCase:
    """Issue #73's primary symptom: one turn, one huge tool result."""
    return CompactionCase(
        name="single_oversized_tool_output",
        description=(
            "A first turn whose only tool result overflows the budget. Only "
            "folding inside the current turn can keep the turn alive."
        ),
        responses=(
            tool_call_response("call-1"),
            finish_response(),
        ),
        tool_output="x" * 900,
        current_instruction="run the tool",
        budget_tokens=1_000,
    )


def user_correction_mid_turn() -> CompactionCase:
    """The instruction being worked on must never be summarized away."""
    return CompactionCase(
        name="user_correction_mid_turn",
        description=(
            "Earlier turns are long enough to be summarized, but the current "
            "instruction is a correction that must reach the model verbatim."
        ),
        responses=(
            tool_call_response("call-1"),
            finish_response(),
        ),
        tool_output="y" * 700,
        current_instruction="no, use the other endpoint instead",
        older_history=(
            "please use the staging endpoint",
            "the staging endpoint is configured",
        ),
        budget_tokens=1_000,
    )


def write_then_continue() -> CompactionCase:
    """An executed effect must be neither lost nor repeated after compaction.

    Two tool batches run before the turn finishes: the first writes, the second
    reads back what it wrote. Compaction may shorten the first result, but the
    fact that it executed must remain and must not be replayed.
    """
    return CompactionCase(
        name="write_then_continue",
        description=(
            "A tool with a side effect runs, the request overflows, compaction "
            "shortens the output, and the turn continues with a second batch."
        ),
        responses=(
            tool_call_response("call-1"),
            tool_call_response("call-2"),
            finish_response(),
        ),
        tool_output="z" * 800,
        current_instruction="write the file then report",
        budget_tokens=1_000,
        expected_tool_runs=2,
    )


def repeated_overflow() -> CompactionCase:
    """Two tool batches in one turn, each pushing the request over again."""
    return CompactionCase(
        name="repeated_overflow",
        description=(
            "The turn overflows twice. Compaction must cope with a second pass "
            "rather than assuming it runs once."
        ),
        responses=(
            tool_call_response("call-1"),
            tool_call_response("call-2"),
            finish_response(),
        ),
        tool_output="w" * 600,
        current_instruction="run the tool twice",
        budget_tokens=1_000,
        expected_tool_runs=2,
    )


def summary_required() -> CompactionCase:
    """A turn whose older history must be summarized, not merely folded.

    Every tool body is small, so folding frees little; the budget is consumed by
    text. Only replacing the older turns with a summary can admit the request,
    which is the path the other cases never reach.
    """
    return CompactionCase(
        name="summary_required",
        description=(
            "The current turn itself carries several large tool results, so "
            "omitting older turns and folding are not enough on their own."
        ),
        responses=(
            tool_call_response("call-1"),
            tool_call_response("call-2"),
            tool_call_response("call-3"),
            finish_response(),
        ),
        tool_output="s" * 700,
        current_instruction="continue from the previous analysis",
        older_history=("earlier analysis " + "a" * 300,),
        # Large enough that a summary request is admissible; a 1_000-token budget
        # cannot even hold the minimum summarizer reserve, which would silently
        # leave this case exercising rule folding only.
        budget_tokens=1_600,
        expected_tool_runs=3,
    )


def offline_compaction_cases() -> tuple[CompactionCase, ...]:
    """Every case, in a fixed order so a run is reproducible."""
    return (
        single_oversized_tool_output(),
        user_correction_mid_turn(),
        write_then_continue(),
        repeated_overflow(),
        summary_required(),
    )


@dataclass(frozen=True, slots=True)
class CaseFacts:
    """What one executed case observed, for assertions and reporting."""

    answer: str
    tool_run_count: int
    conversation: tuple[Message, ...] = ()
    summary_calls: int = 0
    folded_tool_results: int = 1
    extra: dict[str, Any] = field(default_factory=dict)


def tool_facts(conversation: tuple[Message, ...]) -> list[Message]:
    return [message for message in conversation if message.role == "tool"]
