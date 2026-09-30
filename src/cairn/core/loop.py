import asyncio
import json
from typing import Any

from cairn.core.agent import Agent
from cairn.core.budget import (
    BudgetReason,
    RunBudget,
    RunBudgetExceeded,
)
from cairn.core.events import Event
from cairn.core.models import Message, ToolCall, ToolFailure
from cairn.core.permissions import (
    PermissionCapability,
    PermissionResult,
    SessionPermissionHandler,
)
from cairn.observability.models import Span, SpanStatus
from cairn.tools.base import ToolExecutionContext


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
        baseline = SessionPermissionHandler(registered_tools=agent.tools.tools)(
            tool_call
        )
        if baseline.source == "hard_deny":
            permission = baseline
        elif agent.permission_handler is None:
            permission = baseline
            if permission.allowed:
                permission.source = "no_handler"
        else:
            permission = agent.permission_handler(tool_call)
        if permission_span is not None and agent.tracer is not None:
            permission_span.attributes.update(
                {
                    "policy_decision": permission.policy_decision.value,
                    "allowed": permission.allowed,
                    "prompted": permission.prompted,
                    "source": permission.source,
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
    pending_tool_calls: list[ToolCall] = []
    llm_response_count = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0

    try:
        turn_span = None

        # Roll back only while no tool execution has been attempted.
        turn_start = len(agent.state.messages)

        if agent.tracer is not None:
            turn_span = agent.tracer.start_root_span(
                "agent.turn",
                attributes={
                    "max_steps": budget.max_steps,
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

            messages = [Message(role="system", content=agent.system_prompt)]
            if agent.repo_context_provider is not None:
                repo_context = await agent.repo_context_provider.inspect()
                messages.append(
                    Message(role="system", content=repo_context.to_prompt())
                )
            messages.extend(agent.state.messages)

            llm_span = None

            tool_schemas = agent.tools.schemas()

            if agent.tracer is not None and turn_span is not None:
                llm_span = agent.tracer.start_child_span(
                    turn_span,
                    "llm.generate",
                    attributes={
                        "step": step + 1,
                        "message_count": len(messages),
                        "tool_schema_count": len(tool_schemas),
                    },
                )

            try:
                response = await agent.llm.generate(
                    messages,
                    tools=tool_schemas,
                )

            except asyncio.CancelledError:
                input_tokens = None
                output_tokens = None
                if llm_span is not None and agent.tracer is not None:
                    llm_span.attributes["cancelled"] = True
                    agent.tracer.end_span(
                        llm_span,
                        status=SpanStatus.ERROR,
                        error="LLM generation was cancelled.",
                    )
                raise

            except Exception as exc:
                input_tokens = None
                output_tokens = None
                if llm_span is not None and agent.tracer is not None:
                    agent.tracer.end_span(
                        llm_span,
                        status=SpanStatus.ERROR,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                raise

            else:
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
                            llm_span.attributes["input_tokens"] = (
                                response.usage.input_tokens
                            )
                        if response.usage.output_tokens is not None:
                            llm_span.attributes["output_tokens"] = (
                                response.usage.output_tokens
                            )
                    agent.tracer.end_span(
                        llm_span,
                        status=SpanStatus.OK,
                    )

            agent.state.add_assistant_message(
                response.content,
                tool_calls=response.tool_calls,
            )
            pending_tool_calls = response.tool_calls.copy()

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

                permission = _check_tool_permission(agent, tool_call, turn_span)

                if not permission.allowed:
                    tool_content = ToolFailure(
                        error="Permission denied by user.",
                        type="PermissionDenied",
                    ).to_content()

                    agent.state.add_tool_message(
                        tool_call_id=tool_call.id,
                        content=tool_content,
                    )
                    pending_tool_calls.pop(0)

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
                    result = await agent.tools.execute(
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                        context=ToolExecutionContext(
                            network_access=(
                                tool_call.name == "bash"
                                and tool_call.arguments.get("network_access") is True
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
                    pending_tool_calls.pop(0)

                    if tool_span is not None and agent.tracer is not None:
                        tool_span.attributes["cancelled"] = True
                        agent.tracer.end_span(
                            tool_span,
                            status=SpanStatus.ERROR,
                            error="CancelledError: tool execution cancelled",
                        )
                    raise

                except Exception as exc:
                    tool_content = ToolFailure(
                        error=str(exc),
                        type=type(exc).__name__,
                    ).to_content()

                    agent.state.add_tool_message(
                        tool_call_id=tool_call.id,
                        content=tool_content,
                    )
                    pending_tool_calls.pop(0)

                    if tool_span is not None and agent.tracer is not None:
                        tool_span.attributes["error_type"] = type(exc).__name__

                        agent.tracer.end_span(
                            tool_span,
                            status=SpanStatus.ERROR,
                            error=f"{type(exc).__name__}: {exc}",
                        )

                    agent.emit(
                        Event(
                            type="tool_error",
                            data={
                                "tool": tool_call.name,
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                        )
                    )

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
                    pending_tool_calls.pop(0)

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
