"""Server-hosted, single-task, non-interactive entry.

Public entry: ``cairn task run --workspace DIR --prompt TEXT --trust-workspace``
plus the provider-approval facts (``--approve-provider-name/-endpoint/-group/
-key-env/-models``), optional ``--allow-shell``/``--json`` and an optional
``--check-command``. It runs one coding task through the existing
``CodingTaskRunner`` / ``runtime.loop.run_turn`` boundary against an explicit
Workspace and returns a machine-readable result. Check/repair sequencing lives in
``runtime.tasks.check``; this module only parses CLI input and presents results.

Authorization is the operator's explicit CLI facts, never the prompt text, a
model argument or a tool name. An unapproved launch performs no side effect and
never waits for interactive input; a capability the operator did not grant is
denied immediately instead of prompting.
"""

from __future__ import annotations

import asyncio
import math
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import dotenv_values
from pydantic import BaseModel, Field

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.events import Event
from cairn.core.llm import LLMClient
from cairn.core.permissions import PermissionCapability, SessionPermissionHandler
from cairn.core.tasks import TaskCheckStatus, TaskSpec, TaskStatus
from cairn.infra.config import (
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.infra.mcp.config import McpServerConfig, load_mcp_servers
from cairn.infra.observability.sinks import JsonlTraceSink, RedactingTraceSink
from cairn.infra.observability.tracer import Tracer
from cairn.infra.skills import SkillLibrary, layered_skills, resolve_skills
from cairn.infra.tools.base import Tool
from cairn.infra.tools.filesystem_access import normalize_access_root
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.artifacts import atomic_write_text
from cairn.runtime.mcp.activation import open_mcp_servers
from cairn.runtime.model_executor import ModelExecutor
from cairn.runtime.provider_runtime import build_provider_runtime
from cairn.runtime.tasks.check import (
    CheckedTaskOutcome,
    TaskCheckSpec,
    run_task_with_check,
)
from cairn.runtime.tasks.runner import CodingTaskRunner

DEFAULT_MAX_STEPS = 150
DEFAULT_CHECK_TIMEOUT_S = 300.0


class HeadlessStatus(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RUNTIME_ERROR = "runtime_error"
    CANCELLED = "cancelled"
    UNAUTHORIZED = "unauthorized"
    INVALID_REQUEST = "invalid_request"
    CHECK_FAILED = "check_failed"
    CHECK_TIMED_OUT = "check_timed_out"
    CHECK_PERMISSION_DENIED = "check_permission_denied"


class HeadlessResult(BaseModel):
    """Machine-readable outcome of one headless task run.

    ``completed`` means the single turn ended normally, not that the change was
    verified correct: ``verified`` stays false because this entry performs no
    independent acceptance check.
    """

    schema_version: int = 1
    task_id: str
    status: HeadlessStatus
    verified: bool = False
    workspace: str
    final_response: str | None = None
    trace_id: str | None = None
    #: Whether the local JSONL trace was persisted; None when not observed.
    trace_persisted: bool | None = None
    error_category: str | None = None
    error: str | None = None
    permission_denied: bool = False
    denied_tools: list[str] = Field(default_factory=list)
    shell_authorized: bool = False
    #: Whether a host check command was requested.
    check_requested: bool = False
    #: Outcome of the final host check; None when no check was requested.
    check_status: TaskCheckStatus | None = None
    #: Exit status of the final host check; None for a timeout or skipped check.
    check_exit_code: int | None = None
    #: How many times the host check actually ran (0, 1 or 2).
    check_attempts: int = 0


@dataclass(frozen=True, slots=True)
class ProviderApproval:
    """The exact provider facts an operator approves, bound before any credential.

    Binding identity, endpoint, model group, credential *variable name* and the
    full ordered model ids means a changed provider configuration cannot inherit a
    stale approval; a mismatch is rejected before credential resolution or network.
    """

    name: str
    endpoint: str
    group: str
    key_env: str
    model_ids: tuple[str, ...]


class ProviderApprovalRequired(ValueError):
    """The operator did not state the full provider facts to approve."""


class ProviderApprovalMismatch(ValueError):
    """The approved provider facts do not match the configured provider."""


def _redact(text: str | None, secret_values: Sequence[str]) -> str | None:
    if text is None:
        return None
    clean = text
    for value in secret_values:
        if value:
            clean = clean.replace(value, "[redacted]")
    return clean


def _trace_id_of(ref: dict[str, Any] | None) -> str | None:
    value = (ref or {}).get("trace_id")
    return value if isinstance(value, str) and value else None


def _trace_persisted_of(ref: dict[str, Any] | None) -> bool | None:
    value = (ref or {}).get("persisted")
    return value if isinstance(value, bool) else None


def build_headless_permission_handler(
    *,
    workspace_root: Path,
    grant_workspace_trust: bool,
    grant_shell: bool,
    grant_mcp: bool = False,
    grant_network: bool = False,
) -> SessionPermissionHandler:
    """Non-interactive grants only.

    ``prompt``/``prompt_async`` are deliberately left ``None``: an ASK for a
    capability the operator did not grant is denied at once instead of waiting on
    input that will never arrive.
    """
    handler = SessionPermissionHandler(
        prompt=None,
        workspace_root=workspace_root,
        home=Path.home(),
        root_normalizer=normalize_access_root,
    )
    if grant_workspace_trust:
        handler.grants.add(PermissionCapability.WORKSPACE_TRUST)
    if grant_shell:
        handler.grants.add(PermissionCapability.LOCAL_SHELL)
    if grant_mcp:
        handler.grants.add(PermissionCapability.MCP_SERVER)
        handler.grants.add(PermissionCapability.MCP_TOOL)
    if grant_network:
        handler.grants.add(PermissionCapability.NETWORK)
    return handler


def _headless_status(task_status: TaskStatus) -> HeadlessStatus:
    mapping: dict[TaskStatus, HeadlessStatus] = {
        TaskStatus.COMPLETED: HeadlessStatus.COMPLETED,
        TaskStatus.BUDGET_EXHAUSTED: HeadlessStatus.BUDGET_EXHAUSTED,
        TaskStatus.RUNTIME_ERROR: HeadlessStatus.RUNTIME_ERROR,
        TaskStatus.CANCELLED: HeadlessStatus.CANCELLED,
    }
    return mapping[task_status]


def _redacting_tracer(
    tracer: Tracer | None, secret_values: Sequence[str]
) -> Tracer | None:
    """Wrap a tracer so persisted spans never carry a configured credential."""
    values = tuple(value for value in secret_values if value)
    if tracer is None or not values:
        return tracer
    return Tracer(RedactingTraceSink(tracer.sink, values))


def _cancelled_check_state(
    check_ref: dict[str, Any], wants_check: bool
) -> tuple[TaskCheckStatus | None, int]:
    """Observed check state to report when external cancellation is translated."""
    if not wants_check:
        return None, 0
    attempts = int(check_ref.get("attempts", 0) or 0)
    if attempts == 0:
        return TaskCheckStatus.NOT_RUN, 0
    raw = check_ref.get("status")
    valid = {status.value for status in TaskCheckStatus}
    if isinstance(raw, str) and raw in valid and not check_ref.get("running"):
        return TaskCheckStatus(raw), attempts
    # A check was running (never finished) when cancellation arrived.
    return TaskCheckStatus.CANCELLED, attempts


def _outcome_status(outcome: CheckedTaskOutcome) -> HeadlessStatus:
    if outcome.cancelled:
        return HeadlessStatus.CANCELLED
    if outcome.check_permission_denied:
        return HeadlessStatus.CHECK_PERMISSION_DENIED
    task = outcome.task
    if task is None:
        return HeadlessStatus.RUNTIME_ERROR
    if task.status is not TaskStatus.COMPLETED:
        return _headless_status(task.status)
    if outcome.check_status is TaskCheckStatus.FAILED:
        return HeadlessStatus.CHECK_FAILED
    if outcome.check_status is TaskCheckStatus.TIMED_OUT:
        return HeadlessStatus.CHECK_TIMED_OUT
    return HeadlessStatus.COMPLETED


def _result_from_outcome(
    *,
    outcome: CheckedTaskOutcome,
    resolved_id: str,
    resolved_ws: Path,
    ref: dict[str, Any],
    granted_shell: bool,
    check_requested: bool,
    secret_values: Sequence[str],
) -> HeadlessResult:
    task = outcome.task
    status = _outcome_status(outcome)
    error_category: str | None = None
    if outcome.cancelled:
        error_category = "cancelled"
    elif outcome.check_permission_denied:
        error_category = "check_permission_denied"
    elif status is HeadlessStatus.CHECK_FAILED:
        error_category = "check_failed"
    elif status is HeadlessStatus.CHECK_TIMED_OUT:
        error_category = "check_timed_out"
    elif task is not None and task.status is not TaskStatus.COMPLETED:
        error_category = task.status.value
    return HeadlessResult(
        task_id=task.task_id if task is not None else resolved_id,
        status=status,
        verified=outcome.verified,
        workspace=str(resolved_ws),
        final_response=_redact(
            task.final_response if task is not None else None, secret_values
        ),
        trace_id=(task.trace_id if task is not None else None) or _trace_id_of(ref),
        trace_persisted=_trace_persisted_of(ref),
        error_category=error_category,
        error=_redact(task.error if task is not None else None, secret_values),
        permission_denied=bool(outcome.denied_tools),
        denied_tools=list(outcome.denied_tools),
        shell_authorized=granted_shell,
        check_requested=check_requested,
        check_status=outcome.check_status,
        check_exit_code=outcome.check_exit_code,
        check_attempts=outcome.check_attempts,
    )


async def run_headless_task(
    *,
    workspace: Path,
    prompt: str,
    grant_workspace_trust: bool,
    grant_shell: bool,
    llm: LLMClient,
    budget: RunBudget,
    context_budget: ContextBudget | None = None,
    context_builder: ContextBuilder | None = None,
    model_executor: ModelExecutor | None = None,
    task_id: str | None = None,
    tracer: Tracer | None = None,
    secret_env_keys: frozenset[str] = frozenset(),
    secret_values: Sequence[str] = (),
    cancellation_event: asyncio.Event | None = None,
    trace_ref: dict[str, Any] | None = None,
    check_command: str | None = None,
    check_timeout: float = DEFAULT_CHECK_TIMEOUT_S,
    check_ref: dict[str, Any] | None = None,
    skills: SkillLibrary | None = None,
    selected_skills: Sequence[str] = (),
    mcp_configs: Sequence[McpServerConfig] = (),
    grant_mcp: bool = False,
    grant_network: bool = False,
) -> HeadlessResult:
    """Run one task against ``workspace`` and return a machine-readable result.

    An external ``asyncio`` cancellation is *not* converted into a result: the
    runner finishes owned cleanup and the ``CancelledError`` propagates. A
    cooperative ``cancellation_event`` is the caller-requested stop and yields
    ``HeadlessStatus.CANCELLED`` normally.
    """
    resolved_id = task_id or f"task-{uuid.uuid4().hex[:12]}"
    resolved_ws = workspace.resolve()
    # Always observe the trace id/persisted facts internally; also fill the
    # caller's dict when provided so a cancelled CLI run can read them.
    ref: dict[str, Any] = trace_ref if trace_ref is not None else {}
    if not grant_workspace_trust:
        # Refuse before any effect, before building an Agent or provider client.
        return HeadlessResult(
            task_id=resolved_id,
            status=HeadlessStatus.UNAUTHORIZED,
            workspace=str(resolved_ws),
            error_category="workspace_trust_required",
            shell_authorized=grant_shell,
        )
    if not resolved_ws.is_dir():
        return HeadlessResult(
            task_id=resolved_id,
            status=HeadlessStatus.RUNTIME_ERROR,
            workspace=str(resolved_ws),
            error_category="workspace_unavailable",
            shell_authorized=grant_shell,
        )

    resolved_selected: tuple[str, ...] = ()
    if selected_skills:
        if skills is None:
            return HeadlessResult(
                task_id=resolved_id,
                status=HeadlessStatus.INVALID_REQUEST,
                workspace=str(resolved_ws),
                error_category="unknown_skill",
                error="Selected skills require a skill catalog.",
                shell_authorized=grant_shell,
            )
        found, missing = resolve_skills(skills, selected_skills)
        if missing:
            return HeadlessResult(
                task_id=resolved_id,
                status=HeadlessStatus.INVALID_REQUEST,
                workspace=str(resolved_ws),
                error_category="unknown_skill",
                error="Unknown skill: " + ", ".join(missing),
                shell_authorized=grant_shell,
            )
        resolved_selected = tuple(skill.name for skill in found)

    handler = build_headless_permission_handler(
        workspace_root=resolved_ws,
        grant_workspace_trust=grant_workspace_trust,
        grant_shell=grant_shell,
        grant_mcp=grant_mcp,
        grant_network=grant_network,
    )

    def observe(event: Event) -> None:
        # Terminal owns only result correlation (trace id/persisted). Denials and
        # check sequencing are observed inside runtime.tasks.check.
        if event.type in ("trace_start", "trace_finish"):
            trace_id = event.data.get("trace_id")
            if isinstance(trace_id, str) and trace_id:
                ref["trace_id"] = trace_id
            if event.type == "trace_finish":
                persisted = event.data.get("persisted")
                if isinstance(persisted, bool):
                    ref["persisted"] = persisted

    check_spec = (
        TaskCheckSpec(command=check_command, timeout=check_timeout)
        if check_command is not None
        else None
    )
    wants_check_inner = check_command is not None

    async def _run(mcp_tools: Sequence[Tool]) -> CheckedTaskOutcome:
        runner = CodingTaskRunner(
            workspace=Workspace(resolved_ws),
            llm=llm,
            budget=budget,
            context_budget=context_budget,
            context_builder=context_builder,
            model_executor=model_executor,
            permission_handler=handler,
            event_handler=observe,
            tracer=_redacting_tracer(tracer, secret_values),
            secret_env_keys=secret_env_keys,
            skills=skills,
            extra_tools=mcp_tools,
        )
        return await run_task_with_check(
            runner=runner,
            spec=TaskSpec(task_id=resolved_id, prompt=prompt),
            check=check_spec,
            shell_authorized=grant_shell,
            cancellation_event=cancellation_event,
            secret_values=secret_values,
            secret_env_keys=secret_env_keys,
            check_ref=check_ref,
            selected_skills=resolved_selected,
        )

    try:
        if mcp_configs:
            async with open_mcp_servers(
                configs=mcp_configs,
                handler=handler,
                workspace=resolved_ws,
                secret_values=secret_values,
            ) as mcp_runtime:
                outcome = await _run(mcp_runtime.tools)
        else:
            outcome = await _run(())
    except PermissionError as exc:
        return HeadlessResult(
            task_id=resolved_id,
            status=HeadlessStatus.UNAUTHORIZED,
            workspace=str(resolved_ws),
            error_category="mcp_permission_denied",
            error=str(exc),
            shell_authorized=grant_shell,
            check_requested=wants_check_inner,
            check_status=TaskCheckStatus.NOT_RUN if wants_check_inner else None,
        )
    except NotImplementedError as exc:
        return HeadlessResult(
            task_id=resolved_id,
            status=HeadlessStatus.RUNTIME_ERROR,
            workspace=str(resolved_ws),
            error_category="mcp_unsupported",
            error=str(exc),
            shell_authorized=grant_shell,
            check_requested=wants_check_inner,
            check_status=TaskCheckStatus.NOT_RUN if wants_check_inner else None,
        )
    return _result_from_outcome(
        outcome=outcome,
        resolved_id=resolved_id,
        resolved_ws=resolved_ws,
        ref=ref,
        granted_shell=grant_shell,
        check_requested=check_command is not None,
        secret_values=secret_values,
    )


# --- CLI ----------------------------------------------------------------------

task_app = typer.Typer(
    help="Run one non-interactive coding task against an explicit Workspace."
)

_EXIT_CODES: dict[HeadlessStatus, int] = {
    HeadlessStatus.COMPLETED: 0,
    HeadlessStatus.RUNTIME_ERROR: 1,
    HeadlessStatus.BUDGET_EXHAUSTED: 3,
    HeadlessStatus.CANCELLED: 4,
    HeadlessStatus.UNAUTHORIZED: 5,
    HeadlessStatus.INVALID_REQUEST: 2,
    HeadlessStatus.CHECK_FAILED: 7,
    HeadlessStatus.CHECK_TIMED_OUT: 8,
    HeadlessStatus.CHECK_PERMISSION_DENIED: 9,
}


def _exit_code(result: HeadlessResult) -> int:
    # A passing final host check is exit 0 only when no required permission was
    # denied; a denial keeps the run non-zero even if the check itself passed.
    if result.verified:
        return 0
    if result.status is HeadlessStatus.COMPLETED and result.permission_denied:
        return 6
    return _EXIT_CODES[result.status]


def _emit(result: HeadlessResult, *, as_json: bool, report: Path | None) -> None:
    payload = result.model_dump_json(indent=2) + "\n"
    if report is not None:
        atomic_write_text(report, payload)
    if as_json:
        typer.echo(payload, nl=False)
        return
    typer.echo(f"task {result.task_id}: {result.status.value}")
    if result.verified:
        typer.echo("verified: true")
    else:
        typer.echo("verified: false (normal completion is not verification)")
    if result.trace_id:
        typer.echo(f"trace: {result.trace_id}")
    if result.check_requested:
        check = result.check_status.value if result.check_status else "unknown"
        typer.echo(
            f"check: {check} "
            f"(exit={result.check_exit_code}, attempts={result.check_attempts})"
        )
    if result.permission_denied:
        typer.echo(f"permission denied: {', '.join(result.denied_tools)}")
    if result.error_category:
        typer.echo(f"error: {result.error_category}")
    if result.final_response:
        typer.echo("---")
        typer.echo(result.final_response)


def _secret_values(
    secret_env_keys: frozenset[str], env_file_values: dict[str, str | None]
) -> tuple[str, ...]:
    values: list[str] = []
    for key in secret_env_keys:
        value = os.environ.get(key) or env_file_values.get(key)
        if value:
            values.append(value)
    return tuple(values)


def _expected_approval(provider: object, group: object) -> ProviderApproval:
    config = provider.config  # type: ignore[attr-defined]
    return ProviderApproval(
        name=provider.name,  # type: ignore[attr-defined]
        endpoint=config.base_url,
        group=group.name,  # type: ignore[attr-defined]
        key_env=config.api_key_env,
        model_ids=tuple(group.model_ids),  # type: ignore[attr-defined]
    )


def _run_via_config(
    *,
    workspace: Path,
    prompt: str,
    task_id: str | None,
    grant_shell: bool,
    approval: ProviderApproval | None,
    max_steps: int,
    trace_dir: Path | None,
    trace_ref: dict[str, Any] | None = None,
    check_command: str | None = None,
    check_timeout: float = DEFAULT_CHECK_TIMEOUT_S,
    check_ref: dict[str, Any] | None = None,
    selected_skills: Sequence[str] = (),
    mcp_configs: Sequence[McpServerConfig] = (),
    grant_mcp: bool = False,
    grant_network: bool = False,
) -> HeadlessResult:
    """Build the configured provider runtime and run one task.

    The project configuration and dotenv are read from the **target workspace**
    (``<workspace>/.cairn/models.toml`` and ``<workspace>/.env``), not the process
    cwd. The operator's approval is bound to the exact provider identity, endpoint,
    model group, credential variable name and ordered model ids, and a mismatch is
    rejected before resolving the credential. The runtime is built through
    ``build_provider_runtime`` so the real token counter and model fallback
    executor are used.
    """
    env_file_values = dotenv_values(workspace / ".env")
    project = load_project_providers(workspace / ".cairn" / "models.toml")
    provider = default_provider(project, os.environ, env_file_values)
    group = provider.config.model_config[0]
    if approval is None:
        raise ProviderApprovalRequired(
            "Pass --approve-provider-name, --approve-provider-endpoint, "
            "--approve-provider-group, --approve-provider-key-env and "
            "--approve-provider-models to authorize this provider."
        )
    expected = _expected_approval(provider, group)
    if approval != expected:
        raise ProviderApprovalMismatch(
            "approved provider does not match the configured provider "
            f"(approved {approval}; configured {expected})"
        )
    api_key = resolve_provider_api_key(provider.config, os.environ, env_file_values)
    context_budget = resolve_context_budget(os.environ, env_file_values)
    runtime = build_provider_runtime(
        provider, api_key, context_budget, group=group.name
    )
    secret_env_keys = frozenset(
        named.config.api_key_env for named in project.providers or (provider,)
    )
    secret_values = _secret_values(secret_env_keys, env_file_values)
    trace_root = trace_dir if trace_dir is not None else workspace / ".cairn" / "traces"
    tracer = Tracer(JsonlTraceSink(trace_root))
    skills = layered_skills(workspace)
    return asyncio.run(
        run_headless_task(
            workspace=workspace,
            prompt=prompt,
            grant_workspace_trust=True,
            grant_shell=grant_shell,
            llm=runtime.llm,
            budget=RunBudget(max_steps=max_steps),
            context_builder=runtime.context_builder,
            model_executor=runtime.model_executor,
            task_id=task_id,
            tracer=tracer,
            secret_env_keys=secret_env_keys,
            secret_values=secret_values,
            trace_ref=trace_ref,
            check_command=check_command,
            check_timeout=check_timeout,
            check_ref=check_ref,
            skills=skills,
            selected_skills=selected_skills,
            mcp_configs=mcp_configs,
            grant_mcp=grant_mcp,
            grant_network=grant_network,
        )
    )


def _build_approval(
    name: str | None,
    endpoint: str | None,
    group: str | None,
    key_env: str | None,
    models: str | None,
) -> ProviderApproval | None:
    if not all(
        value and value.strip() for value in (name, endpoint, group, key_env, models)
    ):
        return None
    assert name is not None and endpoint is not None and group is not None
    assert key_env is not None and models is not None
    model_ids = tuple(part.strip() for part in models.split(",") if part.strip())
    if not model_ids:
        return None
    return ProviderApproval(
        name=name.strip(),
        endpoint=endpoint.strip(),
        group=group.strip(),
        key_env=key_env.strip(),
        model_ids=model_ids,
    )


def _invalid(
    *,
    workspace: Path,
    task_id: str | None,
    category: str,
    message: str,
    allow_shell: bool,
    as_json: bool,
    report: Path | None,
) -> None:
    """Emit a machine-readable validation failure (invalid_request, exit 2)."""
    result = HeadlessResult(
        task_id=task_id or f"task-{uuid.uuid4().hex[:12]}",
        status=HeadlessStatus.INVALID_REQUEST,
        workspace=str(workspace.resolve()),
        error_category=category,
        error=message,
        shell_authorized=allow_shell,
    )
    _emit(result, as_json=as_json, report=report)


@task_app.command("run")
def task_run(
    workspace: Annotated[
        Path, typer.Option("--workspace", "-w", help="Explicit target Workspace.")
    ],
    prompt: Annotated[str | None, typer.Option("--prompt", help="Task text.")] = None,
    prompt_file: Annotated[
        Path | None, typer.Option("--prompt-file", help="Read task text from a file.")
    ] = None,
    task_id: Annotated[str | None, typer.Option("--task-id")] = None,
    trust_workspace: Annotated[
        bool,
        typer.Option(
            "--trust-workspace",
            help="Explicitly trust the Workspace (required to run).",
        ),
    ] = False,
    approve_provider_name: Annotated[
        str | None, typer.Option("--approve-provider-name")
    ] = None,
    approve_provider_endpoint: Annotated[
        str | None, typer.Option("--approve-provider-endpoint")
    ] = None,
    approve_provider_group: Annotated[
        str | None, typer.Option("--approve-provider-group")
    ] = None,
    approve_provider_key_env: Annotated[
        str | None, typer.Option("--approve-provider-key-env")
    ] = None,
    approve_provider_models: Annotated[
        str | None,
        typer.Option(
            "--approve-provider-models",
            help="Comma-separated, ordered model ids the operator approves.",
        ),
    ] = None,
    allow_shell: Annotated[
        bool,
        typer.Option(
            "--allow-shell",
            help="Grant the local shell capability. Also authorizes the host "
            "--check-command.",
        ),
    ] = False,
    check_command: Annotated[
        str | None,
        typer.Option(
            "--check-command",
            help="Host-selected verification command run in the Workspace after the "
            "task; requires --allow-shell.",
        ),
    ] = None,
    check_timeout: Annotated[
        float,
        typer.Option(
            "--check-timeout",
            help="Seconds before the host check is killed and reported timed out.",
        ),
    ] = DEFAULT_CHECK_TIMEOUT_S,
    skill: Annotated[
        list[str] | None,
        typer.Option(
            "--skill",
            help="Select a project/builtin skill for this task (repeatable).",
        ),
    ] = None,
    mcp: Annotated[
        list[str] | None,
        typer.Option("--mcp", help="Select a configured MCP server (repeatable)."),
    ] = None,
    allow_mcp: Annotated[
        bool,
        typer.Option("--allow-mcp", help="Authorize activating selected MCP servers."),
    ] = False,
    allow_network: Annotated[
        bool,
        typer.Option("--allow-network", help="Authorize an HTTP MCP connection."),
    ] = False,
    max_steps: Annotated[int, typer.Option("--max-steps")] = DEFAULT_MAX_STEPS,
    trace_dir: Annotated[Path | None, typer.Option("--trace-dir")] = None,
    emit_json: Annotated[
        bool, typer.Option("--json", help="Print machine-readable JSON.")
    ] = False,
    report: Annotated[
        Path | None, typer.Option("--report", help="Write the JSON result here.")
    ] = None,
) -> None:
    """Run exactly one task and print its machine-readable outcome."""
    # Admission generates the task id exactly once; the runner and every
    # error/cancellation result carry this same id.
    admitted_id = task_id or f"task-{uuid.uuid4().hex[:12]}"
    trace_ref: dict[str, Any] = {}
    check_ref: dict[str, Any] = {}
    wants_check = check_command is not None
    text = prompt
    if prompt_file is not None:
        try:
            text = prompt_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            # UnicodeError (e.g. invalid UTF-8) is a bad request, not a crash.
            _invalid(
                workspace=workspace,
                task_id=admitted_id,
                category="prompt_file_unreadable",
                message=f"Cannot read --prompt-file: {exc}",
                allow_shell=allow_shell,
                as_json=emit_json,
                report=report,
            )
            raise typer.Exit(2) from None
    if text is None or not text.strip():
        _invalid(
            workspace=workspace,
            task_id=admitted_id,
            category="missing_prompt",
            message="A task prompt is required (--prompt or --prompt-file).",
            allow_shell=allow_shell,
            as_json=emit_json,
            report=report,
        )
        raise typer.Exit(2)
    if max_steps < 1:
        _invalid(
            workspace=workspace,
            task_id=admitted_id,
            category="invalid_max_steps",
            message="--max-steps must be a positive integer.",
            allow_shell=allow_shell,
            as_json=emit_json,
            report=report,
        )
        raise typer.Exit(2)
    resolved_ws = workspace.resolve()
    if not trust_workspace:
        # Refuse without touching provider config, files or the shell.
        result = HeadlessResult(
            task_id=admitted_id,
            status=HeadlessStatus.UNAUTHORIZED,
            workspace=str(resolved_ws),
            error_category="workspace_trust_required",
            shell_authorized=allow_shell,
            check_requested=wants_check,
            check_status=TaskCheckStatus.NOT_RUN if wants_check else None,
        )
        _emit(result, as_json=emit_json, report=report)
        raise typer.Exit(_exit_code(result))
    if wants_check:
        assert check_command is not None
        if not check_command.strip() or "\0" in check_command:
            _invalid(
                workspace=workspace,
                task_id=admitted_id,
                category="invalid_check_command",
                message="--check-command must be a non-empty string.",
                allow_shell=allow_shell,
                as_json=emit_json,
                report=report,
            )
            raise typer.Exit(2)
        # Argument validity precedes the permission decision: a malformed
        # check-command or timeout is rejected as invalid input before shell
        # authorization is consulted.
        if not math.isfinite(check_timeout) or check_timeout <= 0:
            _invalid(
                workspace=workspace,
                task_id=admitted_id,
                category="invalid_check_timeout",
                message="--check-timeout must be a positive number of seconds.",
                allow_shell=allow_shell,
                as_json=emit_json,
                report=report,
            )
            raise typer.Exit(2)
        if not allow_shell:
            result = HeadlessResult(
                task_id=admitted_id,
                status=HeadlessStatus.CHECK_PERMISSION_DENIED,
                workspace=str(resolved_ws),
                error_category="check_permission_denied",
                error="Executing --check-command requires the host shell "
                "authorization --allow-shell.",
                shell_authorized=False,
                check_requested=True,
                check_status=TaskCheckStatus.PERMISSION_DENIED,
            )
            _emit(result, as_json=emit_json, report=report)
            raise typer.Exit(_exit_code(result))
    selected_skill_names: tuple[str, ...] = ()
    if skill:
        found, missing = resolve_skills(layered_skills(resolved_ws), skill)
        if missing:
            _invalid(
                workspace=workspace,
                task_id=admitted_id,
                category="unknown_skill",
                message="Unknown skill: " + ", ".join(missing),
                allow_shell=allow_shell,
                as_json=emit_json,
                report=report,
            )
            raise typer.Exit(2)
        selected_skill_names = tuple(item.name for item in found)
    mcp_configs: tuple[McpServerConfig, ...] = ()
    if mcp:
        try:
            available = load_mcp_servers(resolved_ws)
        except (OSError, ValueError) as exc:
            _invalid(
                workspace=workspace,
                task_id=admitted_id,
                category="mcp_config_error",
                message=f"MCP config error: {exc}",
                allow_shell=allow_shell,
                as_json=emit_json,
                report=report,
            )
            raise typer.Exit(2) from None
        by_name = {server.name: server for server in available}
        missing = [name for name in mcp if name not in by_name]
        if missing:
            _invalid(
                workspace=workspace,
                task_id=admitted_id,
                category="unknown_mcp_server",
                message="Unknown MCP server: " + ", ".join(missing),
                allow_shell=allow_shell,
                as_json=emit_json,
                report=report,
            )
            raise typer.Exit(2)
        selected_servers = tuple(by_name[name] for name in dict.fromkeys(mcp))
        denial = None
        if not allow_mcp:
            denial = "MCP activation requires --allow-mcp."
        elif any(s.transport == "stdio" for s in selected_servers) and not allow_shell:
            denial = "stdio MCP requires --allow-shell."
        elif (
            any(s.transport == "streamable_http" for s in selected_servers)
            and not allow_network
        ):
            denial = "HTTP MCP requires --allow-network."
        if denial is not None:
            result = HeadlessResult(
                task_id=admitted_id,
                status=HeadlessStatus.UNAUTHORIZED,
                workspace=str(resolved_ws),
                error_category="mcp_permission_denied",
                error=denial,
                shell_authorized=allow_shell,
                check_requested=wants_check,
                check_status=TaskCheckStatus.NOT_RUN if wants_check else None,
            )
            _emit(result, as_json=emit_json, report=report)
            raise typer.Exit(_exit_code(result))
        mcp_configs = selected_servers
    approval = _build_approval(
        approve_provider_name,
        approve_provider_endpoint,
        approve_provider_group,
        approve_provider_key_env,
        approve_provider_models,
    )
    try:
        result = _run_via_config(
            workspace=resolved_ws,
            prompt=text,
            task_id=admitted_id,
            grant_shell=allow_shell,
            approval=approval,
            max_steps=max_steps,
            trace_dir=trace_dir,
            trace_ref=trace_ref,
            check_command=check_command if wants_check else None,
            check_timeout=check_timeout,
            check_ref=check_ref,
            selected_skills=selected_skill_names,
            mcp_configs=mcp_configs,
            grant_mcp=allow_mcp,
            grant_network=allow_network,
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        cancel_status, cancel_attempts = _cancelled_check_state(check_ref, wants_check)
        # The library keeps raising external cancellation; only the CLI boundary
        # turns it into a report, after the runner has finished owned cleanup.
        result = HeadlessResult(
            task_id=admitted_id,
            status=HeadlessStatus.CANCELLED,
            workspace=str(resolved_ws),
            trace_id=_trace_id_of(trace_ref),
            trace_persisted=_trace_persisted_of(trace_ref),
            error_category="cancelled",
            shell_authorized=allow_shell,
            check_requested=wants_check,
            check_status=cancel_status,
            check_attempts=cancel_attempts,
        )
    except ProviderApprovalRequired as exc:
        result = HeadlessResult(
            task_id=admitted_id,
            status=HeadlessStatus.UNAUTHORIZED,
            workspace=str(resolved_ws),
            error_category="provider_approval_required",
            error=str(exc),
            shell_authorized=allow_shell,
            check_requested=wants_check,
            check_status=TaskCheckStatus.NOT_RUN if wants_check else None,
        )
    except ProviderApprovalMismatch as exc:
        result = HeadlessResult(
            task_id=admitted_id,
            status=HeadlessStatus.UNAUTHORIZED,
            workspace=str(resolved_ws),
            error_category="provider_approval_mismatch",
            error=str(exc),
            shell_authorized=allow_shell,
            check_requested=wants_check,
            check_status=TaskCheckStatus.NOT_RUN if wants_check else None,
        )
    except (OSError, ValueError) as exc:
        result = HeadlessResult(
            task_id=admitted_id,
            status=HeadlessStatus.RUNTIME_ERROR,
            workspace=str(resolved_ws),
            error_category="configuration_error",
            error=str(exc),
            shell_authorized=allow_shell,
            check_requested=wants_check,
            check_status=TaskCheckStatus.NOT_RUN if wants_check else None,
        )
    _emit(result, as_json=emit_json, report=report)
    raise typer.Exit(_exit_code(result))
