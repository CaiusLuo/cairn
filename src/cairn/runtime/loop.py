"""The one Agent turn.

``run_turn`` owns the turn boundary: it adds the user message, sequences model
calls and tool calls, and decides what happens to the conversation when the turn
ends. Telemetry lives in ``turn_trace``, the per-tool sequence in
``tool_execution``, and candidate selection in ``model_executor``.

Turn flow::

    +-----------------------------------------------------------+
    | run_turn(agent, user_input, budget, session)              |
    +-----------------------------------------------------------+
                                  |
                                  v
    +-----------------------------------------------------------+
    | admission   the exact owning Session; stateless callers   |
    |             skip the guard                                |
    |             open the turn trace; add the user message     |
    +-----------------------------------------------------------+
                                  |
                                  v
    +-----------------------------------------------------------+
    | step, at most budget.max_steps:                           |
    |   build system messages, tool schemas, memory context     |
    |   model_executor.execute()      # candidate fallback      |
    |   add the assistant message; pending = its tool calls     |
    +-----------------------------------------------------------+  <------+
           no tool calls          |             tool calls                |
                 +-----------------------------------+                    |
                 |                |                  |                    |
                 v                |                  v                    |
    +------------------------+    | +--------------------------------+    |
    | agent_finish           |    | | execute_tool_call, each call:  |    |
    | COMPLETED; return      |    | |   preflight -> permission      |    |
    +------------------------+    | |   -> execute -> tool message   |    |
                 |                | +--------------------------------+    |
                 | steps exhausted|                  +--------------------+
                 v                v
    +-----------------------------------------------------------+
    | finally   trace.close(); durable commit; session settle   |
    +-----------------------------------------------------------+

An aborted turn settles the same way for a failure and for a
cancellation:

    +-------------------------------------------------------------+
    | the turn raises, or the caller cancels it                   |
    +-------------------------------------------------------------+
                                   |
                                   v
    +-------------------------------------------------------------+
    | did tool execution start?                                   |
    +-------------------------------------------------------------+
               no |                              yes |
                  v                                  v
    +---------------------------+    +-------------------------------+
    | drop this turn from the   |    | answer every still-pending    |
    | conversation              |    | call with TurnAborted         |
    +---------------------------+    +-------------------------------+
                  +----------------------------------+
                                   v
    +-------------------------------------------------------------+
    | finally   trace.close(); a close failure can also           |
    |           drop an unexecuted turn; durable commit;          |
    |           session settle                                    |
    +-------------------------------------------------------------+
"""

import asyncio
from collections import deque

from cairn.core.budget import (
    BudgetReason,
    RunBudget,
    RunBudgetExceeded,
)
from cairn.core.context import ContextBudgetExceeded
from cairn.core.events import Event
from cairn.core.memory.context import MemoryContext
from cairn.core.models import ToolCall, ToolFailure
from cairn.runtime.agent import Agent
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.session import Session, SessionAdmissionError, TurnOutcome
from cairn.runtime.tool_execution import execute_tool_call
from cairn.runtime.turn.context import build_system_messages
from cairn.runtime.turn.hook import (
    compact_before_next_step,
    reusable_record,
)
from cairn.runtime.turn.steering import SteerQueue
from cairn.runtime.turn_trace import AttemptContext, TurnTrace


def _context_limit(agent: Agent) -> int | None:
    """The input budget a status line should measure against, when it is known."""
    builder = getattr(agent, "context_builder", None)
    budget = getattr(builder, "budget", None)
    limit = getattr(budget, "input_tokens", None)
    return limit if type(limit) is int and limit > 0 else None


async def run_turn(
    agent: Agent,
    user_input: str,
    *,
    budget: RunBudget,
    session: Session | None = None,
    steering: SteerQueue | None = None,
) -> str:
    """Run one Agent turn.

    ``steering`` carries instructions that arrive while the turn is running. They
    join this same turn at a step boundary rather than starting a new one, because
    a redirect is meant to keep the work already done.
    """
    # Admission is synchronous and precedes every trace/event/state/effect.
    if agent.session_owner is not session:
        raise SessionAdmissionError("The exact owning Session is required.")
    durable = session if isinstance(session, DurableSession) else None
    if durable is not None:
        durable._admit_durable(agent, user_input, budget)
    elif session is not None:
        session._admit(agent)
    else:
        agent._stateless_turns += 1
    outcome = TurnOutcome.RUNTIME_ERROR
    trace_finalization_failed = False
    primary_error: BaseException | None = None
    trace: TurnTrace | None = None
    try:
        if session is not None:
            session._started()
        trace_error: str | None = None
        tool_execution_started = False
        pending_tool_calls: deque[ToolCall] = deque()
        trace = TurnTrace(agent, budget)
        if session is not None:
            # Opaque ids attach only after admission, so a rejected turn records
            # no attribution it never owned.
            trace.correlate(session.trace_correlation())

        try:
            # Roll back only while no tool execution has been attempted.
            turn_start = len(agent.state.messages)
            trace.start()

            agent.state.add_user_message(user_input)

            for step in range(budget.max_steps):
                # Instructions that arrived during the previous step join the
                # conversation here, before the request is built. This is the only
                # safe boundary: the tool batch has settled, so no call group is
                # split, and compaction below sees the new message as real context.
                if steering is not None:
                    for pending in steering.take_steers():
                        agent.state.add_user_message(pending.text)

                # The context budget travels with the existing step event rather
                # than a new event type: the event vocabulary is a closed
                # contract that other consumers read, and a status line needs
                # only data, not a new kind of thing to handle.
                agent.emit(
                    Event(
                        type="agent_step",
                        data={
                            "step": step + 1,
                            "max_steps": budget.max_steps,
                            "context_limit": _context_limit(agent),
                        },
                    )
                )

                system_messages = await build_system_messages(agent, session=session)
                tool_schemas = agent.tools.schemas()
                memory_context = None
                if agent.memory_reader is not None:
                    try:
                        memory_context = await agent.memory_reader.retrieve(user_input)
                    except Exception:
                        # No arbitrary memory/IO diagnostics reach the trace. External
                        # cancellation propagates through the existing turn recovery.
                        memory_context = MemoryContext(read_error=True)

                trace.begin_step(
                    AttemptContext(
                        step=step + 1,
                        tool_schema_count=len(tool_schemas),
                        system_message_count=len(system_messages),
                        memory=memory_context,
                    )
                )

                def on_stream(event: Event, current_step: int = step + 1) -> None:
                    # Transient text belongs to presentation, never AgentState or
                    # spans. Attempt numbering is local to this existing step.
                    agent.emit(
                        Event(
                            type=event.type, data={**event.data, "step": current_step}
                        )
                    )

                try:
                    _, response = await agent.model_executor.execute(
                        primary_runtime=(agent.llm, agent.context_builder),
                        system_messages=system_messages,
                        history=agent.state.messages,
                        current_turn_start=turn_start,
                        tools=tool_schemas,
                        project_memory=memory_context,
                        compaction=reusable_record(agent, agent.state.messages),
                        on_request=trace.on_request,
                        on_fallback=trace.on_failure,
                        on_stream=on_stream,
                    )
                except (asyncio.CancelledError, Exception) as exc:
                    # Cancellation and provider failures both finalize the attempt.
                    trace.on_failure(exc)
                    raise
                else:
                    trace.on_response(response)

                agent.state.add_assistant_message(
                    response.content,
                    tool_calls=response.tool_calls,
                )
                pending_tool_calls = deque(response.tool_calls)

                if not response.tool_calls:
                    # A message can arrive between the model finishing and this
                    # check. Returning here would discard it and leave the operator
                    # believing they redirected work that continued unchanged.
                    if steering is not None and steering.has_pending_steers:
                        continue
                    agent.emit(
                        Event(
                            type="agent_finish",
                            data={
                                "content": response.content,
                                "step": step + 1,
                            },
                        )
                    )

                    outcome = TurnOutcome.COMPLETED
                    return response.content or ""

                def mark_tool_execution_started() -> None:
                    nonlocal tool_execution_started
                    tool_execution_started = True

                def consume_tool_call(
                    pending: deque[ToolCall] = pending_tool_calls,
                ) -> None:
                    pending.popleft()

                assistant_index = len(agent.state.messages) - 1
                for tool_call in response.tool_calls:

                    def before_execute(
                        call: ToolCall = tool_call, index: int = assistant_index
                    ) -> None:
                        assert durable is not None
                        durable._before_execute(call, index)

                    await execute_tool_call(
                        agent,
                        tool_call,
                        trace.turn_span,
                        mark_tool_execution_started=mark_tool_execution_started,
                        consume_tool_call=consume_tool_call,
                        before_execute=before_execute if durable is not None else None,
                    )

                # Every call in this batch has settled, so the conversation is
                # whole and the next request is the first one that can be
                # reduced. Compaction is an optimisation: a failure here leaves
                # the conversation untouched and must not end the turn.
                await compact_before_next_step(
                    agent,
                    system_messages=system_messages,
                    tool_schemas=tool_schemas,
                    turn_start=turn_start,
                    trace=trace,
                )

            agent.emit(
                Event(
                    type="agent_budget_exhausted",
                    data={
                        "reason": BudgetReason.MAX_STEPS.value,
                        "limit": budget.max_steps,
                        "used": budget.max_steps,
                    },
                )
            )

            raise RunBudgetExceeded(
                reason=BudgetReason.MAX_STEPS,
                limit=budget.max_steps,
                used=budget.max_steps,
            )

        except asyncio.CancelledError as exc:
            primary_error = exc
            trace_error = "CancelledError: turn cancelled"
            _settle_aborted_turn(
                agent,
                pending_tool_calls,
                tool_execution_started,
                turn_start,
                "Tool was not executed because the turn was cancelled.",
            )
            raise

        except Exception as exc:
            primary_error = exc
            trace_error = f"{type(exc).__name__}: {exc}"
            _settle_aborted_turn(
                agent,
                pending_tool_calls,
                tool_execution_started,
                turn_start,
                f"Tool was not executed because the turn aborted: {trace_error}",
            )
            raise

        finally:
            try:
                trace.close(trace_error)
            except BaseException as finalization_error:
                trace_finalization_failed = True
                if (
                    isinstance(finalization_error, Exception) or session is not None
                ) and not tool_execution_started:
                    del agent.state.messages[turn_start:]
                if session is None or primary_error is None:
                    raise
                primary_error.add_note("Turn trace finalization failed.")
    except BaseException as exc:
        primary_error = exc
        if isinstance(exc, asyncio.CancelledError):
            outcome = TurnOutcome.CANCELLED
        elif isinstance(exc, (RunBudgetExceeded, ContextBudgetExceeded)):
            outcome = TurnOutcome.BUDGET_EXHAUSTED
        else:
            outcome = TurnOutcome.RUNTIME_ERROR
        raise
    finally:
        durability_error: BaseException | None = None
        if durable is not None:
            try:
                durable._commit(
                    outcome,
                    None
                    if trace_finalization_failed or trace is None
                    else trace.summary,
                )
            except BaseException as exc:
                durability_error = exc
        try:
            if session is not None:
                session._settle(outcome, trace_finalization_failed)
            else:
                agent._stateless_turns -= 1
        except BaseException as exc:
            if durable is None:
                raise
            if durability_error is None:
                durability_error = exc
        if durability_error is not None:
            if primary_error is None:
                raise durability_error
            primary_error.add_note("Durable session requires recovery.")


def _settle_aborted_turn(
    agent: Agent,
    pending_tool_calls: deque[ToolCall],
    tool_execution_started: bool,
    turn_start: int,
    error: str,
) -> None:
    """Roll back the turn, or close out every call it left pending.

    A turn that never reached tool execution is invisible in the conversation; a
    turn that did must answer each outstanding tool call, so the next model call
    never sees a dangling ``tool_call`` id.
    """
    if not tool_execution_started:
        del agent.state.messages[turn_start:]
        return
    for tool_call in pending_tool_calls:
        agent.state.add_tool_message(
            tool_call_id=tool_call.id,
            content=ToolFailure(error=error, type="TurnAborted").to_content(),
        )
