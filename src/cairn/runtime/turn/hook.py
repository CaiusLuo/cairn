"""Applying compaction to a live Agent: the per-step hook and the manual entry.

This module owns when a compaction runs against an Agent and what happens to the
accepted record. It does not own the turn: ``run_turn`` still sequences steps and
decides rollback, and it calls the hook here between a settled tool batch and the
next request. Keeping the wiring out of the loop leaves that module as the single
place that reads as one turn.
"""

import asyncio
from typing import Any

from cairn.core.compaction import CompactionRecord, ReusableCompaction, StaleCompaction
from cairn.core.models import Message
from cairn.runtime.agent import Agent
from cairn.runtime.turn.compaction import (
    CompactionInput,
    CompactionOutcome,
    CompactionStep,
    RequestCompactor,
    compact,
)
from cairn.runtime.turn.context import build_system_messages
from cairn.runtime.turn_trace import TurnTrace


async def run_compaction(
    agent: Agent,
    *,
    summarize_budget: int | None = None,
) -> CompactionOutcome:
    """Compact the conversation on a caller's explicit request.

    Unlike the per-step hook this ignores the budget threshold, so a user can ask
    for a reduction at any time. It still refuses to act while a tool call is
    pending, because the tool groups would not be whole and the request that
    folds them does not exist yet.

    Repeating the command is idempotent: while the accepted compaction still
    matches the conversation it is reused, so asking twice costs nothing and
    cannot summarize already-summarized history a second time.
    """
    remembered = agent.compaction
    if remembered is not None and remembered.is_current_for(agent.state.messages):
        # Nothing has happened since the last accepted compaction, so there is
        # nothing to reduce and no reason to pay for the same summary again.
        return _already_compacted(remembered.record)

    outcome = await _reduce(
        agent,
        system_messages=await build_system_messages(agent),
        tools=agent.tools.schemas(),
        turn_start=_current_turn_start(agent.state.messages),
        summarize_budget=summarize_budget,
        forced=True,
    )
    remember(agent, outcome)
    return outcome


def _already_compacted(record: CompactionRecord) -> CompactionOutcome:
    """Report an existing, still-valid compaction instead of repeating the work."""
    return CompactionOutcome(
        step=CompactionStep.NOT_NEEDED,
        record=record,
        tokens_before=record.folded_tokens,
        tokens_after=record.tokens_after,
    )


def _current_turn_start(messages: list[Message]) -> int:
    """Where the open turn begins, or len(messages) when no turn is open.

    A turn that ended in an assistant answer is closed: a manual compaction
    between turns may summarize all of it, so the boundary sits past the end. A
    turn whose last message is not an answer is still open and is protected.
    """
    if not messages or messages[-1].role == "assistant":
        return len(messages)
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            return index
    return len(messages)


def reusable_record(agent: Agent, history: list[Message]) -> CompactionRecord | None:
    """Return the accepted compaction while it still matches ``history``.

    A record that no longer matches is dropped rather than repaired: the next
    compaction recomputes it from the complete conversation, which is intact.
    """
    remembered = agent.compaction
    if remembered is None:
        return None
    if not remembered.reusable_with(history):
        return None
    try:
        return remembered.record_for(history)
    except StaleCompaction:
        return None


async def compact_before_next_step(
    agent: Agent,
    *,
    system_messages: list[Message],
    tool_schemas: list[dict[str, Any]],
    turn_start: int,
    trace: TurnTrace,
) -> None:
    """Reduce the next request after a settled tool batch.

    Runs between steps, never inside a response: the boundary it relies on is
    that no tool call is pending, so it cannot split a tool group or touch the
    current turn's own message.
    """
    try:
        outcome = await _reduce(
            agent,
            system_messages=system_messages,
            tools=tool_schemas,
            turn_start=turn_start,
            summarize_budget=None,
            forced=False,
        )
    except asyncio.CancelledError:
        # Cancellation keeps its existing owner: no record is written here.
        raise
    except Exception:
        # Compaction must never convert a usable turn into a failed one.
        return
    trace.on_compaction(outcome)
    remember(agent, outcome)


async def _reduce(
    agent: Agent,
    *,
    system_messages: list[Message],
    tools: list[dict[str, Any]],
    turn_start: int,
    summarize_budget: int | None,
    forced: bool,
) -> CompactionOutcome:
    """Run one reduction against this Agent's current budget and conversation."""
    builder = agent.context_builder
    compactor = RequestCompactor(counter=builder.counter, budget=builder.budget)
    return await compact(
        source=CompactionInput(
            messages=agent.state.messages,
            turn_start=turn_start,
            system_messages=system_messages,
            tools=tools,
        ),
        compactor=compactor,
        summarizer=agent.summarizer,
        summarize_budget=(
            compactor.policy.summarizer_reserve
            if summarize_budget is None
            else summarize_budget
        ),
        forced=forced,
    )


def remember(agent: Agent, outcome: CompactionOutcome) -> None:
    """Keep an accepted substitution for later turns; ignore a degraded attempt."""
    if outcome.record is not None:
        agent.compaction = ReusableCompaction.capture(
            agent.state.messages, outcome.record
        )
