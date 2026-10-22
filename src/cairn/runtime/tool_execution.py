"""Tool-call execution boundary for one Agent turn.

``run_turn`` owns the turn lifecycle and conversation rollback. This module owns the
per-tool sequence inside an already accepted assistant response: schema preflight,
permission evaluation, execution authority, model-facing tool messages, events and
per-tool spans.

One call::

    +---------------------------------------------------------+
    | execute_tool_call(agent, tool_call, turn_span)          |
    +---------------------------------------------------------+
                                 |
                                 v
    +---------------------------------------------------------+
    | 1. preflight   get_tool + validate(arguments)           |
    |    before any approval is requested                     |
    +---------------------------------------------------------+
                                 |  ToolNotFound or InvalidArguments:
                                 +--> tool error message; consume; return
                                 |
                                 v
    +---------------------------------------------------------+
    | 2. permission   evaluate_permission_policy()            |
    |    ASK -> handler (no handler fails closed)             |
    |    DENY or not allowed -> failure; consume; return      |
    +---------------------------------------------------------+
                                 |
                                 v
    +---------------------------------------------------------+
    | 3. before_execute(), when the session supplies one      |
    |    durable admission; a failure escapes to run_turn     |
    +---------------------------------------------------------+
                                 |
                                 v
    +---------------------------------------------------------+
    | 4. mark_tool_execution_started(); tool.execute(...)     |
    +---------------------------------------------------------+
                                 |
          +-----------------------+-----------------------+
          | cancelled             | exception             | result
          v                       v                       v
    +----------------------+  +----------------------+  +----------------------+
    | ToolCancelled        |  | tool error message   |  | JSON tool message    |
    | message; consume;    |  | and span; consume    |  | consume; then emit   |
    | re-raise             |  | return               |  | tool_result          |
    +----------------------+  +----------------------+  +----------------------+
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from cairn.core.events import Event
from cairn.core.models import ToolCall, ToolFailure
from cairn.core.permissions import (
    AccessKind,
    PermissionCapability,
    PermissionDecision,
    PermissionRequest,
    PermissionResult,
    PermissionSource,
    evaluate_permission_policy,
)
from cairn.infra.observability.models import Span, SpanStatus
from cairn.infra.tools.base import (
    InvalidArguments,
    Tool,
    ToolExecutionContext,
    ToolExecutionFailure,
    ToolFailureKind,
    ToolNotFound,
)
from cairn.runtime.agent import Agent


def _ask_for_approval(agent: Agent, tool_call: ToolCall) -> PermissionResult:
    """Ask the approval handler for the requested capability.

    Without a handler the call fails closed: there is nobody who could grant the
    authority the command asked for. The handler derives the capability from the
    tool call's validated arguments, so authority never crosses from here.
    """
    if agent.permission_handler is None:
        return PermissionResult(
            policy_decision=PermissionDecision.ASK,
            allowed=False,
            source=PermissionSource.NO_HANDLER,
        )

    return agent.permission_handler(tool_call)


def _resolve_ask(
    agent: Agent, tool: Tool | None, tool_call: ToolCall
) -> PermissionResult:
    """Turn one ASK decision into a concrete answer.

    A tool reports the capability its arguments need. When it reports one, the
    handler decides that concrete request; when it reports none, the call is an
    ordinary operation inside the Workspace and is allowed without a prompt. A
    ``bash`` call reports nothing because its capability is the shell itself.
    """
    request = _tool_access_request(tool, tool_call) if tool is not None else None
    if request is not None:
        decided = _file_tool_decision(agent, request)
        if decided is not None:
            return decided
        return _ask_for_approval(agent, tool_call)

    if tool_call.name == "bash":
        return _ask_for_approval(agent, tool_call)

    # A file tool with nothing to report stays inside the Workspace.
    return PermissionResult(
        policy_decision=PermissionDecision.ASK,
        allowed=True,
        source=PermissionSource.BASELINE,
    )


def _tool_access_request(tool: Tool, tool_call: ToolCall) -> PermissionRequest | None:
    """The filesystem request a validated file tool reports, if any.

    A tool that can reach outside the Workspace reports the capability it needs
    from its own arguments. A broken report never widens authority: it degrades
    to ``None`` and the tool then refuses the path itself.
    """
    report = getattr(tool, "requested_capability", None)
    if not callable(report):
        return None
    try:
        request = report(tool_call.arguments)
    except Exception:
        return None
    if request is None:
        return None
    return PermissionRequest(
        capability=request.capability,
        justification=request.justification,
        tool_call=tool_call,
        paths=request.paths,
        access=request.access,
    )


def _file_tool_decision(
    agent: Agent, request: PermissionRequest
) -> PermissionResult | None:
    """Route a file tool's request to the handler's path-aware decision.

    Only a handler that owns workspace and grant state can answer it; any other
    handler is left to the ordinary capability path.
    """
    handler = agent.permission_handler
    decide = getattr(handler, "access_decision", None)
    if not callable(decide):
        return None
    # The tool reports the file and the access it wants; the handler grants the
    # containing directory so one approval covers that subtree.
    access = request.access if request.access is not None else AccessKind.WRITE
    return cast(
        PermissionResult,
        decide(
            outside=True,
            access=access,
            path=Path(request.paths[0]),
        ),
    )


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
    *,
    failure_kind: ToolFailureKind = ToolFailureKind.EFFECT_UNKNOWN,
) -> None:
    """Record an expected preflight or execution failure through one path."""
    if isinstance(error, ToolExecutionFailure):
        outcome = error.outcome
        failure = ToolFailure(error=outcome.message, type=outcome.error_type)
        failure_kind = outcome.kind
    else:
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
        tool_span.attributes.update(
            {
                "error_type": failure.type,
                "failure_kind": failure_kind.value,
            }
        )
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
                "failure_kind": failure_kind.value,
            },
        )
    )


def _accessible_roots(permission: PermissionResult) -> tuple[Path, ...]:
    """The filesystem roots a granted capability authorizes for this execution.

    An empty tuple unless ``FILESYSTEM_ACCESS`` was actually granted, so a tool
    never receives roots from a capability it did not get.
    """
    if PermissionCapability.FILESYSTEM_ACCESS not in permission.granted_capabilities:
        return ()
    return tuple(Path(root) for root in sorted(permission.granted_roots))


def _check_tool_permission(
    agent: Agent,
    tool_call: ToolCall,
    turn_span: Span | None,
    tool: Tool | None = None,
) -> PermissionResult:
    # A span exists only when a tracer does, so one local binding is enough.
    tracer = agent.tracer
    permission_span = None

    if tracer is not None and turn_span is not None:
        permission_span = tracer.start_child_span(
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
            permission = _resolve_ask(agent, tool, tool_call)

        if permission_span is not None and tracer is not None:
            permission_span.attributes.update(
                {
                    "policy_decision": permission.policy_decision.value,
                    "allowed": permission.allowed,
                    "prompted": permission.prompted,
                    "source": permission.source.value,
                    "granted_capabilities": sorted(permission.granted_capabilities),
                    "granted_roots": sorted(permission.granted_roots),
                }
            )
    except Exception as exc:
        if permission_span is not None and tracer is not None:
            tracer.end_span(
                permission_span,
                status=SpanStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )
        raise
    else:
        if permission_span is not None and tracer is not None:
            tracer.end_span(permission_span, status=SpanStatus.OK)

    return permission


async def execute_tool_call(
    agent: Agent,
    tool_call: ToolCall,
    turn_span: Span | None,
    *,
    mark_tool_execution_started: Callable[[], None],
    consume_tool_call: Callable[[], None],
    before_execute: Callable[[], None] | None = None,
) -> None:
    """Validate, authorize and execute one tool call from the current turn.

    The caller owns the pending-call collection used for turn rollback. This
    function calls ``consume_tool_call`` exactly when it has appended the required
    model-facing tool message for this call; unhandled implementation failures
    intentionally leave the call pending so ``run_turn`` can decide whether to
    roll back the whole turn or add ``TurnAborted`` messages.
    """
    agent.emit(
        Event(
            type="tool_call",
            data={
                "tool_call_id": tool_call.id,
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
        consume_tool_call()
        _record_tool_error(
            agent,
            tool_call,
            turn_span,
            exc,
            failure_kind=ToolFailureKind.PERMANENT,
        )
        return

    permission = _check_tool_permission(agent, tool_call, turn_span, tool)

    if permission.policy_decision is PermissionDecision.DENY or not permission.allowed:
        failure = _permission_failure(permission)
        tool_content = failure.to_content()

        agent.state.add_tool_message(
            tool_call_id=tool_call.id,
            content=tool_content,
        )
        consume_tool_call()
        agent.emit(
            Event(
                type="tool_denied",
                data={
                    "tool": tool_call.name,
                    "error": failure.error,
                    "error_type": failure.type,
                    "failure_kind": ToolFailureKind.PERMISSION_DENIED.value,
                },
            )
        )

        return

    # Persistence admission is not a tool error. Failure escapes to the turn
    # owner before any executable span, execution marker or external effect.
    if before_execute is not None:
        before_execute()

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
        mark_tool_execution_started()
        result = await tool.execute(
            arguments=tool_call.arguments,
            # Authority comes only from the approved capability, never from the
            # model-provided arguments.
            context=ToolExecutionContext(
                accessible_roots=_accessible_roots(permission),
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
        consume_tool_call()

        if tool_span is not None and agent.tracer is not None:
            tool_span.attributes.update(
                {
                    "cancelled": True,
                    "failure_kind": ToolFailureKind.CANCELLED.value,
                }
            )
            agent.tracer.end_span(
                tool_span,
                status=SpanStatus.ERROR,
                error="CancelledError: tool execution cancelled",
            )
        raise

    except Exception as exc:
        consume_tool_call()
        kind = (
            ToolFailureKind.DEADLINE
            if isinstance(exc, TimeoutError)
            else ToolFailureKind.PERMISSION_DENIED
            if isinstance(exc, PermissionError)
            else ToolFailureKind.EFFECT_UNKNOWN
        )
        _record_tool_error(
            agent,
            tool_call,
            turn_span,
            exc,
            tool_span,
            failure_kind=kind,
        )

        return
    else:
        tool_content = json.dumps(
            result.model_dump(),
            ensure_ascii=False,
        )

        agent.state.add_tool_message(
            tool_call_id=tool_call.id,
            content=tool_content,
        )
        consume_tool_call()

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
                # Identifies which call this answers. The display pairs a call
                # with its result, and a tool name alone cannot: the same tool
                # can run several times in one turn.
                "tool_call_id": tool_call.id,
                "tool": tool_call.name,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
            },
        )
    )
