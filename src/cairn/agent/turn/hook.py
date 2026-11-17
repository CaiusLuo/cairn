"""Applying compaction to a live Agent: the per-step hook and the manual entry.

This module owns when a compaction runs against an Agent and what happens to the
accepted record. It does not own the turn: ``run_turn`` still sequences steps and
decides rollback, and it calls the hook here before each request, while no tool
call is pending. Keeping the wiring out of the loop leaves that module as the
single place that reads as one turn.
"""

import asyncio
from typing import Any

from cairn.agent.context.compaction import (
    CompactionRecord,
    ReusableCompaction,
    StaleCompaction,
)
from cairn.agent.harness import Agent
from cairn.agent.models.messages import Message
from cairn.agent.session.session import Session
from cairn.agent.turn.compaction import (
    CompactionInput,
    CompactionOutcome,
    CompactionStep,
    RequestCompactor,
    compact,
    summarizer_policy_version,
)
from cairn.agent.turn.context import build_system_messages
from cairn.agent.turn.trace import TurnTrace
from cairn.shared.lifecycle import await_owned


async def run_compaction(
    agent: Agent,
    *,
    summarize_budget: int | None = None,
    session: Session | None = None,
) -> CompactionOutcome:
    """Compact the conversation on a caller's explicit request.

    Unlike the per-step hook this ignores the budget threshold, so a user can ask
    for a reduction at any time. It still refuses to act while a tool call is
    pending, because the tool groups would not be whole and the request that
    folds them does not exist yet.

    ``session`` is the binding this Agent's next request would run under, and it
    contributes the same system context ``run_turn`` would send. A caller that
    compacts between turns passes its Session explicitly rather than leaving the
    measurement to infer one.

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
        system_messages=await build_system_messages(agent, session=session),
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


async def compact_before_request(
    agent: Agent,
    *,
    system_messages: list[Message],
    tool_schemas: list[dict[str, Any]],
    turn_start: int,
    trace: TurnTrace,
    remaining_tokens: int | None = None,
) -> None:
    """Reduce the request that is about to be sent, at any step.

    Runs before every model request, not only after a settled tool batch: a long
    history that opens a new turn is exactly when the request is largest, and
    leaving it to the context builder's whole-turn omission would send a notice
    instead of the facts. No tool call is pending at this boundary, so the
    current turn's own messages stay protected and no group is split.

    ``remaining_tokens`` is the turn budget's leftover ceiling. It can only lower
    the summary allowance; a summary that cannot be paid for is skipped rather
    than issued and then discovered to be unaffordable.
    """
    reducing = asyncio.create_task(
        _reduce(
            agent,
            system_messages=system_messages,
            tools=tool_schemas,
            turn_start=turn_start,
            summarize_budget=None,
            remaining_tokens=remaining_tokens,
            forced=False,
        )
    )
    try:
        outcome = await asyncio.shield(reducing)
    except asyncio.CancelledError:
        # The measurement reads the conversation in a worker thread, and a
        # summary may already be in flight. A turn rollback must not trim the
        # list under it, so finish observing the work before the cancellation
        # leaves this hook.
        await await_owned(reducing)
        if not reducing.cancelled() and reducing.exception() is None:
            # The summary completed and was paid for even though the turn is
            # ending. Its usage belongs to this turn's ledger; the record itself
            # is not kept, because the rollback is about to shorten the history
            # it was captured against.
            finished = reducing.result()
            if finished.step is not CompactionStep.NOT_NEEDED:
                trace.on_compaction(finished)
        raise
    except Exception:
        # Compaction must never convert a usable turn into a failed one.
        return
    if outcome.step is CompactionStep.NOT_NEEDED:
        # The request already fits. Running this check before every request is
        # intentional, so an unchanged measurement is not a compaction outcome.
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
    remaining_tokens: int | None = None,
) -> CompactionOutcome:
    """Run one reduction against this Agent's current budget and conversation."""
    builder = agent.context_builder
    compactor = RequestCompactor(counter=builder.counter, budget=builder.budget)
    allowance = (
        compactor.policy.summarizer_reserve
        if summarize_budget is None
        else summarize_budget
    )
    summarizer = agent.summarizer
    if remaining_tokens is not None:
        allowance = min(allowance, remaining_tokens)
        if allowance < 1:
            # The turn budget cannot pay for a summary request. Rule folding may
            # still reduce the view; offering a summarizer would spend money the
            # budget has already accounted for elsewhere.
            summarizer = None
    return await compact(
        source=CompactionInput(
            messages=agent.state.messages,
            turn_start=turn_start,
            system_messages=system_messages,
            tools=tools,
            previous=reusable_record(agent, agent.state.messages),
        ),
        compactor=compactor,
        summarizer=summarizer,
        summarize_budget=max(1, allowance),
        forced=forced,
    )


def remember(agent: Agent, outcome: CompactionOutcome) -> None:
    """Keep an accepted substitution for later turns; ignore a degraded attempt."""
    if outcome.record is not None:
        agent.compaction = ReusableCompaction.capture(
            agent.state.messages,
            outcome.record,
            policy_version=summarizer_policy_version(agent.summarizer),
        )
