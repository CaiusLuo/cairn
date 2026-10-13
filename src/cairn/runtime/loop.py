"""The one Agent turn.

``run_turn`` owns the turn boundary: it adds the user message, sequences model
calls and tool calls, and decides what happens to the conversation when the turn
ends. Telemetry lives in ``turn_trace``, the per-tool sequence in
``tool_execution``, and candidate selection in ``model_executor``.
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
from cairn.core.models import Message, ToolCall, ToolFailure
from cairn.runtime.agent import Agent
from cairn.runtime.session import Session, SessionAdmissionError, TurnOutcome
from cairn.runtime.tool_execution import execute_tool_call
from cairn.runtime.turn_trace import AttemptContext, TurnTrace


async def run_turn(
    agent: Agent,
    user_input: str,
    *,
    budget: RunBudget,
    session: Session | None = None,
) -> str:
    # Admission is synchronous and precedes every trace/event/state/effect.
    if agent.session_owner is not session:
        raise SessionAdmissionError("The exact owning Session is required.")
    if session is not None:
        session._admit(agent)
    else:
        agent._stateless_turns += 1
    outcome = TurnOutcome.RUNTIME_ERROR
    trace_finalization_failed = False
    try:
        if session is not None:
            session._started()
        trace_error: str | None = None
        primary_error: BaseException | None = None
        tool_execution_started = False
        pending_tool_calls: deque[ToolCall] = deque()
        trace = TurnTrace(agent, budget)

        try:
            # Roll back only while no tool execution has been attempted.
            turn_start = len(agent.state.messages)
            trace.start()

            agent.state.add_user_message(user_input)

            for step in range(budget.max_steps):
                agent.emit(
                    Event(
                        type="agent_step",
                        data={
                            "step": step + 1,
                            "max_steps": budget.max_steps,
                        },
                    )
                )

                system_messages = [Message(role="system", content=agent.system_prompt)]
                if agent.repo_context_provider is not None:
                    repo_context = await agent.repo_context_provider.inspect()
                    system_messages.append(
                        Message(role="system", content=repo_context.to_prompt())
                    )
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

                for tool_call in response.tool_calls:
                    await execute_tool_call(
                        agent,
                        tool_call,
                        trace.turn_span,
                        mark_tool_execution_started=mark_tool_execution_started,
                        consume_tool_call=consume_tool_call,
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
        if isinstance(exc, asyncio.CancelledError):
            outcome = TurnOutcome.CANCELLED
        elif isinstance(exc, (RunBudgetExceeded, ContextBudgetExceeded)):
            outcome = TurnOutcome.BUDGET_EXHAUSTED
        else:
            outcome = TurnOutcome.RUNTIME_ERROR
        raise
    finally:
        if session is not None:
            session._settle(outcome, trace_finalization_failed)
        else:
            agent._stateless_turns -= 1


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
