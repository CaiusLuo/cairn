import asyncio
import json
from collections import deque
from typing import Any

from cairn.core.agent import Agent
from cairn.core.budget import (
    BudgetReason,
    RunBudget,
    RunBudgetExceeded,
)
from cairn.core.context import ContextBudget, ContextRequest
from cairn.core.events import Event
from cairn.core.models import LLMResponse, Message, ToolCall, ToolFailure
from cairn.core.permissions import (
    PermissionCapability,
    PermissionDecision,
    PermissionResult,
    PermissionSource,
    evaluate_permission_policy,
)
from cairn.llm.model_manager import ModelConfig
from cairn.observability.models import Span, SpanStatus
from cairn.tools.base import InvalidArguments, ToolExecutionContext, ToolNotFound


def _provider_attributes(agent: Agent) -> dict[str, str]:
    """Trace-only provider identity, empty for provider-neutral agents."""
    if agent.provider_name is None:
        return {}
    return {"provider": agent.provider_name}


def _ask_for_approval(agent: Agent, tool_call: ToolCall) -> PermissionResult:
    """Ask the approval handler for the requested capability.

    Without a handler the call fails closed: there is nobody who could grant the
    authority the command asked for.
    """
    if agent.permission_handler is None:
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=False,
            source=PermissionSource.NO_HANDLER,
        )

    return agent.permission_handler(tool_call)


def _permission_failure(permission: PermissionResult) -> ToolFailure:
    """Translate a non-allowed permission result into a model-facing failure.

    The failure type tells the model what actually happened: a policy guardrail,
    a missing approval handler, or a user denial. They must never be collapsed
    into "the user denied it".
    """
    if permission.source == PermissionSource.POLICY_DENY:
        return ToolFailure(
            error=permission.reason or "Denied by the action policy.",
            type="PolicyDenied",
        )

    if permission.source == PermissionSource.NO_HANDLER:
        return ToolFailure(
            error=(
                "Capability approval is required, but no permission handler "
                "is configured."
            ),
            type="PermissionRequired",
        )

    return ToolFailure(error="Permission denied by user.", type="PermissionDenied")


def _record_tool_error(
    agent: Agent,
    tool_call: ToolCall,
    turn_span: Span | None,
    error: Exception,
    tool_span: Span | None = None,
) -> None:
    """Record an expected preflight or execution failure through one path."""
    failure = ToolFailure(error=str(error), type=type(error).__name__)

    agent.state.add_tool_message(
        tool_call_id=tool_call.id,
        content=failure.to_content(),
    )

    if agent.tracer is not None and turn_span is not None:
        if tool_span is None:
            # Execution failures already have a span opened before the call.
            tool_span = agent.tracer.start_child_span(
                turn_span,
                "tool.preflight",
                attributes={
                    "tool": tool_call.name,
                    "tool_call_id": tool_call.id,
                },
            )
        tool_span.attributes["error_type"] = failure.type
        agent.tracer.end_span(
            tool_span,
            status=SpanStatus.ERROR,
            error=f"{failure.type}: {failure.error}",
        )

    agent.emit(
        Event(
            type="tool_error",
            data={
                "tool": tool_call.name,
                "error": failure.error,
                "error_type": failure.type,
            },
        )
    )


def _check_tool_permission(
    agent: Agent,
    tool_call: ToolCall,
    turn_span: Span | None,
) -> PermissionResult:
    permission_span = None

    if agent.tracer is not None and turn_span is not None:
        permission_span = agent.tracer.start_child_span(
            turn_span,
            "permission.check",
            attributes={
                "tool": tool_call.name,
                "tool_call_id": tool_call.id,
                "handler_configured": agent.permission_handler is not None,
            },
        )

    try:
        # Pure policy first. Baseline ALLOW and hard DENY never reach a handler,
        # so a permissive handler can never re-authorize a policy denial.
        permission = evaluate_permission_policy(tool_call)
        if permission.policy_decision is PermissionDecision.ASK:
            permission = _ask_for_approval(agent, tool_call)

        if permission_span is not None and agent.tracer is not None:
            permission_span.attributes.update(
                {
                    "policy_decision": permission.policy_decision.value,
                    "allowed": permission.allowed,
                    "prompted": permission.prompted,
                    "source": permission.source.value,
                    "granted_capabilities": sorted(permission.granted_capabilities),
                }
            )
    except Exception as exc:
        if permission_span is not None and agent.tracer is not None:
            agent.tracer.end_span(
                permission_span,
                status=SpanStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )
        raise
    else:
        if permission_span is not None and agent.tracer is not None:
            agent.tracer.end_span(permission_span, status=SpanStatus.OK)

    return permission


async def run_turn(
    agent: Agent,
    user_input: str,
    *,
    budget: RunBudget,
) -> str:
    trace_error: str | None = None
    tool_execution_started = False
    pending_tool_calls: deque[ToolCall] = deque()
    llm_response_count = 0
    reported_omitted_turns = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    llm_span: Span | None = None
    llm_attempt_started = False

    def start_attempt(
        request: ContextRequest,
        context_budget: ContextBudget,
        model: ModelConfig | None,
        model_id: str | None,
        attempt: int,
    ) -> None:
        """Open a span and record admission metadata for one provider call."""
        nonlocal llm_span, llm_attempt_started, reported_omitted_turns
        llm_attempt_started = True
        if request.omitted_turns and request.omitted_turns != reported_omitted_turns:
            agent.emit(
                Event(
                    type="context_trimmed",
                    data={
                        "omitted_turns": request.omitted_turns,
                        "omitted_messages": request.omitted_messages,
                    },
                )
            )
        reported_omitted_turns = request.omitted_turns

        if agent.tracer is None or turn_span is None:
            return
        attributes: dict[str, Any] = {
            "step": step + 1,
            "tool_schema_count": len(tool_schemas),
            "message_count": len(request.messages),
            "message_count_before": len(system_messages) + len(agent.state.messages),
            "context_tokens_before": request.tokens_before.tokens,
            "context_tokens_after": request.tokens_after.tokens,
            "context_count_is_estimate": request.tokens_after.is_estimate,
            "context_max_tokens": context_budget.max_tokens,
            "context_response_tokens": context_budget.response_tokens,
            "context_omitted_turns": request.omitted_turns,
            "context_omitted_messages": request.omitted_messages,
            **_provider_attributes(agent),
        }
        if model is not None:
            attributes.update(model=model_id, model_name=model.name, attempt=attempt)
        llm_span = agent.tracer.start_child_span(
            turn_span, "llm.generate", attributes=attributes
        )

    def fail_attempt(exc: BaseException) -> None:
        """Close the attempt span after a provider call produced no response."""
        nonlocal llm_span, llm_attempt_started, input_tokens, output_tokens
        # No attempt means admission failed before any provider call, so usage
        # already accounted for by earlier steps must survive.
        if not llm_attempt_started:
            return
        llm_attempt_started = False
        input_tokens = None
        output_tokens = None
        if llm_span is not None and agent.tracer is not None:
            if isinstance(exc, asyncio.CancelledError):
                llm_span.attributes["cancelled"] = True
                error = "LLM generation was cancelled."
            else:
                error = f"{type(exc).__name__}: {exc}"
            agent.tracer.end_span(llm_span, status=SpanStatus.ERROR, error=error)
        # A fallback closes this attempt before the next one starts.
        llm_span = None

    def finish_attempt(response: LLMResponse) -> None:
        nonlocal llm_response_count, input_tokens, output_tokens
        llm_response_count += 1
        if input_tokens is not None:
            if response.usage is None or response.usage.input_tokens is None:
                input_tokens = None
            else:
                input_tokens += response.usage.input_tokens
        if output_tokens is not None:
            if response.usage is None or response.usage.output_tokens is None:
                output_tokens = None
            else:
                output_tokens += response.usage.output_tokens

        if llm_span is not None and agent.tracer is not None:
            llm_span.attributes["tool_call_count"] = len(response.tool_calls)
            llm_span.attributes["has_content"] = response.content is not None
            if response.usage is not None:
                if response.usage.input_tokens is not None:
                    llm_span.attributes["input_tokens"] = response.usage.input_tokens
                if response.usage.output_tokens is not None:
                    llm_span.attributes["output_tokens"] = response.usage.output_tokens
            agent.tracer.end_span(llm_span, status=SpanStatus.OK)

    try:
        turn_span = None

        # Roll back only while no tool execution has been attempted.
        turn_start = len(agent.state.messages)

        if agent.tracer is not None:
            turn_span = agent.tracer.start_root_span(
                "agent.turn",
                attributes={
                    "max_steps": budget.max_steps,
                    "context_max_tokens": agent.context_builder.budget.max_tokens,
                    "context_response_tokens": agent.context_builder.budget.response_tokens,
                    **_provider_attributes(agent),
                },
            )

            agent.emit(
                Event(
                    type="trace_start",
                    data={"trace_id": turn_span.context.trace_id},
                )
            )

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
            llm_span = None
            llm_attempt_started = False

            try:
                _, response = await agent.model_executor.execute(
                    agent=agent,
                    system_messages=system_messages,
                    history=agent.state.messages,
                    current_turn_start=turn_start,
                    tools=tool_schemas,
                    on_request=start_attempt,
                    on_fallback=fail_attempt,
                )
            except (asyncio.CancelledError, Exception) as exc:
                # Cancellation and provider failures both finalize the attempt.
                fail_attempt(exc)
                raise
            else:
                finish_attempt(response)

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

                return response.content or ""

            for tool_call in response.tool_calls:
                agent.emit(
                    Event(
                        type="tool_call",
                        data={
                            "tool": tool_call.name,
                            "arguments": tool_call.arguments,
                        },
                    )
                )

                # Existence and validity must precede any authority approval.
                try:
                    tool = agent.tools.get_tool(tool_call.name)
                    tool.validate(tool_call.arguments)
                except (ToolNotFound, InvalidArguments) as exc:
                    pending_tool_calls.popleft()
                    _record_tool_error(agent, tool_call, turn_span, exc)
                    continue

                permission = _check_tool_permission(agent, tool_call, turn_span)

                if (
                    permission.policy_decision is PermissionDecision.DENY
                    or not permission.allowed
                ):
                    failure = _permission_failure(permission)
                    tool_content = failure.to_content()

                    agent.state.add_tool_message(
                        tool_call_id=tool_call.id,
                        content=tool_content,
                    )
                    pending_tool_calls.popleft()
                    agent.emit(
                        Event(
                            type="tool_denied",
                            data={
                                "tool": tool_call.name,
                                "error": failure.error,
                                "error_type": failure.type,
                            },
                        )
                    )

                    continue

                tool_span = None

                if agent.tracer is not None and turn_span is not None:
                    tool_span = agent.tracer.start_child_span(
                        turn_span,
                        "tool.execute",
                        attributes={
                            "tool": tool_call.name,
                            "tool_call_id": tool_call.id,
                        },
                    )

                try:
                    tool_execution_started = True
                    result = await tool.execute(
                        arguments=tool_call.arguments,
                        # Authority comes only from the approved capability, never
                        # from the model-provided arguments.
                        context=ToolExecutionContext(
                            network_access=(
                                tool_call.name == "bash"
                                and PermissionCapability.NETWORK
                                in permission.granted_capabilities
                            )
                        ),
                    )

                except asyncio.CancelledError:
                    tool_content = ToolFailure(
                        error="Tool execution was cancelled.",
                        type="ToolCancelled",
                    ).to_content()

                    agent.state.add_tool_message(
                        tool_call_id=tool_call.id,
                        content=tool_content,
                    )
                    pending_tool_calls.popleft()

                    if tool_span is not None and agent.tracer is not None:
                        tool_span.attributes["cancelled"] = True
                        agent.tracer.end_span(
                            tool_span,
                            status=SpanStatus.ERROR,
                            error="CancelledError: tool execution cancelled",
                        )
                    raise

                except Exception as exc:
                    pending_tool_calls.popleft()
                    _record_tool_error(agent, tool_call, turn_span, exc, tool_span)

                    continue
                else:
                    tool_content = json.dumps(
                        result.model_dump(),
                        ensure_ascii=False,
                    )

                    agent.state.add_tool_message(
                        tool_call_id=tool_call.id,
                        content=tool_content,
                    )
                    pending_tool_calls.popleft()

                    if tool_span is not None and agent.tracer is not None:
                        tool_span.attributes.update(
                            {
                                "exit_code": result.exit_code,
                                "stdout_length": len(result.stdout),
                                "stderr_length": len(result.stderr),
                                "stdout_truncated": result.stdout_truncated,
                                "stderr_truncated": result.stderr_truncated,
                            }
                        )
                        if result.exit_code == 0:
                            agent.tracer.end_span(
                                tool_span,
                                status=SpanStatus.OK,
                            )
                        else:
                            agent.tracer.end_span(
                                tool_span,
                                status=SpanStatus.ERROR,
                                error=f"ToolError: exit_code {result.exit_code}",
                            )

                agent.emit(
                    Event(
                        type="tool_result",
                        data={
                            "tool": tool_call.name,
                            "exit_code": result.exit_code,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                            "stdout_truncated": result.stdout_truncated,
                            "stderr_truncated": result.stderr_truncated,
                        },
                    )
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

    except asyncio.CancelledError:
        trace_error = "CancelledError: turn cancelled"
        if not tool_execution_started:
            del agent.state.messages[turn_start:]
        else:
            for tool_call in pending_tool_calls:
                agent.state.add_tool_message(
                    tool_call_id=tool_call.id,
                    content=ToolFailure(
                        error="Tool was not executed because the turn was cancelled.",
                        type="TurnAborted",
                    ).to_content(),
                )
        raise

    except Exception as exc:
        trace_error = f"{type(exc).__name__}: {exc}"
        if not tool_execution_started:
            del agent.state.messages[turn_start:]
        else:
            for tool_call in pending_tool_calls:
                agent.state.add_tool_message(
                    tool_call_id=tool_call.id,
                    content=ToolFailure(
                        error=f"Tool was not executed because the turn aborted: {trace_error}",
                        type="TurnAborted",
                    ).to_content(),
                )
        raise

    finally:
        if turn_span is not None and agent.tracer is not None:
            status = SpanStatus.OK if trace_error is None else SpanStatus.ERROR
            turn_input_tokens = input_tokens if llm_response_count else None
            turn_output_tokens = output_tokens if llm_response_count else None
            if turn_input_tokens is not None:
                turn_span.attributes["input_tokens"] = turn_input_tokens
            if turn_output_tokens is not None:
                turn_span.attributes["output_tokens"] = turn_output_tokens
            try:
                agent.tracer.end_span(
                    turn_span,
                    status=status,
                    error=trace_error,
                )

                persistence_error = agent.tracer.pop_persistence_error(
                    turn_span.context.trace_id
                )

                trace_finish_data: dict[str, Any] = {
                    "trace_id": turn_span.context.trace_id,
                    "status": status.value,
                    "persisted": persistence_error is None,
                    "persistence_error": persistence_error,
                }
                usage: dict[str, int] = {}
                if turn_input_tokens is not None:
                    usage["input_tokens"] = turn_input_tokens
                if turn_output_tokens is not None:
                    usage["output_tokens"] = turn_output_tokens
                if usage:
                    trace_finish_data["usage"] = usage

                agent.emit(
                    Event(
                        type="trace_finish",
                        data=trace_finish_data,
                    )
                )
            except Exception:
                if not tool_execution_started:
                    del agent.state.messages[turn_start:]
                raise
