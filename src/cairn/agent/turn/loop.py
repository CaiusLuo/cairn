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
from collections.abc import Awaitable, Sequence
from dataclasses import asdict
from typing import Any, Literal, Protocol, TypedDict

from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from cairn.agent.budget import (
    BudgetReason,
    RunBudget,
    RunBudgetExceeded,
)
from cairn.agent.context.window import ContextBudgetExceeded
from cairn.agent.harness import Agent
from cairn.agent.models.messages import Message, ToolCall, ToolFailure
from cairn.agent.session.durable import DurableSession
from cairn.agent.session.session import Session, SessionAdmissionError, TurnOutcome
from cairn.agent.turn.budget import TurnBudgetPolicy
from cairn.agent.turn.context import build_system_messages
from cairn.agent.turn.events import Event
from cairn.agent.turn.hook import (
    compact_before_next_step,
    reusable_record,
)
from cairn.agent.turn.steering import SteerQueue
from cairn.agent.turn.tools import execute_tool_call
from cairn.agent.turn.trace import AttemptContext, TurnTrace
from cairn.shared.lifecycle import settle_owned


class _GraphState(TypedDict):
    step: int
    done: bool
    answer: str


class _GraphNodeFailure(Exception):
    """Keep framework annotations off the original public exception."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        super().__init__("Agent graph node failed")


class _GraphNode(Protocol):
    def __call__(self, state: _GraphState) -> Awaitable[_GraphState]: ...


def _guard_node(node: _GraphNode) -> _GraphNode:
    async def invoke(state: _GraphState) -> _GraphState:
        try:
            return await node(state)
        except Exception as error:
            raise _GraphNodeFailure(error) from None

    return invoke


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
    selected_skills: Sequence[str] = (),
    message_id: str = "",
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
    if durable is not None and selected_skills:
        raise SessionAdmissionError("Skill selection is not supported in durable mode.")
    if durable is not None:
        durable._admit_durable(agent, user_input, budget)
    elif session is not None:
        session._admit(agent)
    else:
        agent._stateless_turns += 1
    budget_policy = TurnBudgetPolicy(budget, agent.emit)
    agent.last_budget_usage = None
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

            selected_context: list[str] = []
            for name in dict.fromkeys(selected_skills):
                selected = (
                    agent.skill_library.read(name) if agent.skill_library else None
                )
                if selected is None:
                    raise ValueError(f"Unknown selected skill: {name}")
                selected_context.append(
                    f"[Selected skill: {selected.name}]\n\n{selected.body}"
                )
            # One user message keeps selected instructions with the task during
            # compaction; external instructions never become system authority.
            agent.state.add_user_message("\n\n".join((*selected_context, user_input)))

            system_messages: list[Message] = []
            tool_schemas: list[dict[str, Any]] = []

            async def model_node(state: _GraphState) -> _GraphState:
                nonlocal pending_tool_calls, outcome, system_messages, tool_schemas
                assert trace is not None
                budget_policy.check()
                step = state["step"]
                budget_policy.steps = step + 1
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
                            "budget": asdict(budget_policy.snapshot()),
                        },
                    )
                )

                system_messages = await build_system_messages(agent, session=session)
                tool_schemas = agent.tools.schemas()

                trace.begin_step(
                    AttemptContext(
                        step=step + 1,
                        tool_schema_count=len(tool_schemas),
                        system_message_count=len(system_messages),
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

                def on_request(*args: Any) -> None:
                    assert trace is not None
                    budget_policy.check()
                    budget_policy.requests += 1
                    trace.on_request(*args)

                def on_fallback(exc: Exception) -> None:
                    assert trace is not None
                    budget_policy.known = False
                    trace.on_failure(exc)

                try:
                    _, response = await agent.model_executor.execute(
                        primary_runtime=(agent.llm, agent.context_builder),
                        system_messages=system_messages,
                        history=agent.state.messages,
                        current_turn_start=turn_start,
                        tools=tool_schemas,
                        compaction=reusable_record(agent, agent.state.messages),
                        on_request=on_request,
                        on_fallback=on_fallback,
                        on_stream=on_stream,
                    )
                except (asyncio.CancelledError, Exception) as exc:
                    # Cancellation and provider failures both finalize the attempt.
                    trace.on_failure(exc)
                    raise
                else:
                    trace.on_response(response)
                    budget_policy.observe_response(response)

                if durable is not None:
                    try:
                        durable._check_response_capacity(response.tool_calls)
                    except RunBudgetExceeded as exc:
                        budget_policy.stop(exc.reason, exc.limit, exc.used)
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
                        return {"step": step + 1, "done": False, "answer": ""}
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
                    return {
                        "step": step + 1,
                        "done": True,
                        "answer": response.content or "",
                    }

                return {"step": step + 1, "done": False, "answer": ""}

            async def tool_node(state: _GraphState) -> _GraphState:
                nonlocal outcome
                assert trace is not None
                budget_policy.check()
                accepted_calls = tuple(pending_tool_calls)

                def mark_tool_execution_started() -> None:
                    nonlocal tool_execution_started
                    tool_execution_started = True
                    budget_policy.tool_attempts += 1

                def consume_tool_call(
                    pending: deque[ToolCall] = pending_tool_calls,
                ) -> None:
                    pending.popleft()

                assistant_index = len(agent.state.messages) - 1
                for tool_call in accepted_calls:
                    budget_policy.check()

                    def before_execute(
                        call: ToolCall = tool_call, index: int = assistant_index
                    ) -> None:
                        assert durable is not None
                        try:
                            durable._before_execute(call, index)
                        except RunBudgetExceeded as exc:
                            budget_policy.stop(exc.reason, exc.limit, exc.used)

                    await execute_tool_call(
                        agent,
                        tool_call,
                        trace.turn_span,
                        mark_tool_execution_started=mark_tool_execution_started,
                        consume_tool_call=consume_tool_call,
                        before_execute=before_execute if durable is not None else None,
                        trusted_user_text=user_input,
                        trusted_message_id=message_id,
                    )

                # A host continuation may hand off only after the entire tool
                # group has settled, through this same turn/receipt boundary.
                if (
                    agent.after_tools is not None
                    and (answer := agent.after_tools()) is not None
                ):
                    agent.state.add_assistant_message(answer)
                    outcome = TurnOutcome.COMPLETED
                    agent.emit(
                        Event(
                            type="agent_finish",
                            data={"content": answer, "step": state["step"]},
                        )
                    )
                    return {**state, "done": True, "answer": answer}

                budget_policy.observe_batch(
                    accepted_calls,
                    agent.state.messages[assistant_index + 1 :],
                    agent.permission_handler,
                )
                budget_policy.check()

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

                return state

            def after_model(state: _GraphState) -> Literal["model", "tools", "end"]:
                if state["done"]:
                    return "end"
                if pending_tool_calls:
                    return "tools"
                return "model" if state["step"] < budget.max_steps else "end"

            def after_tools(state: _GraphState) -> Literal["model", "end"]:
                if state["done"]:
                    return "end"
                return "model" if state["step"] < budget.max_steps else "end"

            graph = StateGraph(_GraphState)
            graph.add_node("model", _guard_node(model_node))
            graph.add_node("tools", _guard_node(tool_node))
            graph.add_edge(START, "model")
            graph.add_conditional_edges(
                "model", after_model, {"model": "model", "tools": "tools", "end": END}
            )
            graph.add_conditional_edges(
                "tools", after_tools, {"model": "model", "end": END}
            )
            compiled = graph.compile()
            # LangGraph schedules nodes; this existing turn remains the sole
            # admission/fence/rollback/commit owner. No graph retry/checkpointer.
            with tracing_context(enabled=False):
                execution = asyncio.create_task(
                    compiled.ainvoke(
                        {"step": 0, "done": False, "answer": ""},
                        config={"recursion_limit": 2 * budget.max_steps + 2},
                    )
                )
                deadline = asyncio.timeout(budget_policy.remaining_seconds())
                try:
                    async with deadline:
                        graph_result = await asyncio.shield(execution)
                except _GraphNodeFailure as exc:
                    await settle_owned(execution)
                    raise exc.error from None
                except NodeCancelledError as exc:
                    await settle_owned(execution)
                    # LangGraph wraps cancellation raised by a node; Cairn's
                    # public turn API keeps its existing cancellation contract.
                    if isinstance(exc.__cause__, asyncio.CancelledError):
                        raise exc.__cause__ from None
                    raise
                except BaseException as primary:
                    await settle_owned(execution)
                    # A shielded caller cancellation must not erase a model or
                    # tool cleanup failure. Task callers retain their own stop
                    # policy above this boundary, just as with direct awaits.
                    try:
                        execution.result()
                    except asyncio.CancelledError as owned:
                        for note in getattr(owned, "__notes__", ()):
                            primary.add_note(note)
                    except NodeCancelledError as owned:
                        for note in getattr(owned.__cause__, "__notes__", ()):
                            primary.add_note(note)
                    except BaseException as failure:
                        if isinstance(failure, _GraphNodeFailure):
                            failure = failure.error
                        if (
                            isinstance(primary, (asyncio.CancelledError, TimeoutError))
                            and failure is not primary
                        ):
                            raise failure from None
                    if isinstance(primary, TimeoutError) and deadline.expired():
                        assert budget.max_seconds is not None
                        budget_policy.stop(
                            BudgetReason.MAX_SECONDS,
                            budget.max_seconds,
                            budget_policy.snapshot().elapsed_seconds,
                        )
                    raise
            if graph_result["done"]:
                return str(graph_result["answer"])

            budget_policy.stop(
                BudgetReason.MAX_STEPS, budget.max_steps, budget.max_steps
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
        agent.last_budget_usage = budget_policy.snapshot()
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
