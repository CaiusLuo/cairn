import json

from cairn.core.agent import Agent
from cairn.core.events import Event
from cairn.core.models import Message
from cairn.observability.models import SpanStatus


async def run_turn(
    agent: Agent,
    user_input: str,
    max_steps: int = 20,
) -> str:
    trace_error: str | None = None

    try:
        turn_span = None

        if agent.tracer is not None:
            turn_span = agent.tracer.start_root_span(
                "agent.turn",
                attributes={
                    "max_steps": max_steps,
                },
            )

        agent.state.add_user_message(user_input)

        for step in range(max_steps):
            agent.emit(
                Event(
                    type="agent_step",
                    data={
                        "step": step + 1,
                        "max_steps": max_steps,
                    },
                )
            )

            messages = [
                Message(role="system", content=agent.system_prompt),
                *agent.state.messages,
            ]

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

            except Exception as exc:
                if llm_span is not None and agent.tracer is not None:
                    agent.tracer.end_span(
                        llm_span,
                        status=SpanStatus.ERROR,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                raise

            else:
                if llm_span is not None and agent.tracer is not None:
                    llm_span.attributes["tool_call_count"] = len(response.tool_calls)
                    llm_span.attributes["has_content"] = response.content is not None
                    agent.tracer.end_span(
                        llm_span,
                        status=SpanStatus.OK,
                    )

            agent.state.add_assistant_message(
                response.content,
                tool_calls=response.tool_calls,
            )

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

                if agent.permission_handler is None:
                    if permission_span is not None and agent.tracer is not None:
                        permission_span.attributes.update(
                            {
                                "allowed": True,
                                "source": "no_handler",
                            }
                        )
                        agent.tracer.end_span(permission_span, status=SpanStatus.OK)
                else:
                    try:
                        permission = agent.permission_handler(tool_call)
                        if permission_span is not None and agent.tracer is not None:
                            permission_span.attributes.update(
                                {
                                    "policy_decision": permission.policy_decision.value,
                                    "allowed": permission.allowed,
                                    "prompted": permission.prompted,
                                }
                            )
                        allowed = permission.allowed
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

                    if not allowed:
                        tool_content = json.dumps(
                            {
                                "error": "Permission denied by user.",
                                "type": "PermissionDenied",
                            },
                            ensure_ascii=False,
                        )

                        agent.state.add_tool_message(
                            tool_call_id=tool_call.id,
                            content=tool_content,
                        )

                        continue

                try:
                    result = await agent.tools.execute(
                        name=tool_call.name,
                        arguments=tool_call.arguments,
                    )

                except Exception as exc:
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

                    tool_content = json.dumps(
                        {
                            "err": str(exc),
                            "type": type(exc).__name__,
                        },
                        ensure_ascii=False,
                    )

                    agent.state.add_tool_message(
                        tool_call_id=tool_call.id,
                        content=tool_content,
                    )

                    continue

                agent.emit(
                    Event(
                        type="tool_result",
                        data={
                            "tool": tool_call.name,
                            "exit_code": result.exit_code,
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                        },
                    )
                )

                tool_content = json.dumps(
                    result.model_dump(),
                    ensure_ascii=False,
                )

                agent.state.add_tool_message(
                    tool_call_id=tool_call.id,
                    content=tool_content,
                )

        agent.emit(
            Event(
                type="agent_step_limit",
                data={
                    "max_steps": max_steps,
                },
            )
        )

        raise RuntimeError(f"Agent exceeded maximum steps: {max_steps}")

    except Exception as exc:
        trace_error = f"{type(exc).__name__}: {exc}"
        raise

    finally:
        if turn_span is not None and agent.tracer is not None:
            agent.tracer.end_span(
                turn_span,
                status=SpanStatus.ERROR if trace_error is not None else SpanStatus.OK,
                error=trace_error,
            )
