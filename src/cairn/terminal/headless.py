"""Server-hosted, single-task, non-interactive entry.

Public entry: ``cairn task run --workspace DIR --prompt TEXT --trust-workspace``
plus the provider-approval facts (``--approve-provider-name/-endpoint/-group/
-key-env/-models``) and optional ``--allow-shell``/``--json``. It runs exactly one
coding task through the existing ``CodingTaskRunner`` / ``runtime.loop.run_turn``
boundary against an explicit Workspace and returns a machine-readable result.

Authorization is the operator's explicit CLI facts, never the prompt text, a
model argument or a tool name. An unapproved launch performs no side effect and
never waits for interactive input; a capability the operator did not grant is
denied immediately instead of prompting.
"""

from __future__ import annotations

import asyncio
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
from cairn.core.tasks import TaskSpec, TaskStatus
from cairn.infra.config import (
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.infra.observability.sinks import JsonlTraceSink, RedactingTraceSink
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.filesystem_access import normalize_access_root
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.artifacts import atomic_write_text
from cairn.runtime.model_executor import ModelExecutor
from cairn.runtime.provider_runtime import build_provider_runtime
from cairn.runtime.tasks.runner import CodingTaskRunner

DEFAULT_MAX_STEPS = 150


class HeadlessStatus(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RUNTIME_ERROR = "runtime_error"
    CANCELLED = "cancelled"
    UNAUTHORIZED = "unauthorized"
    INVALID_REQUEST = "invalid_request"


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

    handler = build_headless_permission_handler(
        workspace_root=resolved_ws,
        grant_workspace_trust=grant_workspace_trust,
        grant_shell=grant_shell,
    )
    denied: set[str] = set()

    def observe(event: Event) -> None:
        if event.type in ("trace_start", "trace_finish"):
            trace_id = event.data.get("trace_id")
            if isinstance(trace_id, str) and trace_id:
                ref["trace_id"] = trace_id
            if event.type == "trace_finish":
                persisted = event.data.get("persisted")
                if isinstance(persisted, bool):
                    ref["persisted"] = persisted
        elif event.type == "tool_denied":
            name = event.data.get("tool")
            if isinstance(name, str) and name:
                denied.add(name)

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
    )
    # External cancellation propagates (the runner has already settled owned
    # work); only a normal return is mapped to a result.
    result = await runner.run(
        TaskSpec(task_id=resolved_id, prompt=prompt),
        cancellation_event=cancellation_event,
    )
    return HeadlessResult(
        task_id=result.task_id,
        status=_headless_status(result.status),
        workspace=str(resolved_ws),
        final_response=_redact(result.final_response, secret_values),
        trace_id=result.trace_id or _trace_id_of(ref),
        trace_persisted=_trace_persisted_of(ref),
        error_category=(
            None if result.status is TaskStatus.COMPLETED else result.status.value
        ),
        error=_redact(result.error, secret_values),
        permission_denied=bool(denied),
        denied_tools=sorted(denied),
        shell_authorized=grant_shell,
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
}


def _exit_code(result: HeadlessResult) -> int:
    # A completed run in which a capability was denied is distinguishable from a
    # fully-authorized completion without changing the underlying status.
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
        typer.Option("--allow-shell", help="Grant the local shell capability."),
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
        )
        _emit(result, as_json=emit_json, report=report)
        raise typer.Exit(_exit_code(result))
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
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
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
        )
    except ProviderApprovalRequired as exc:
        result = HeadlessResult(
            task_id=admitted_id,
            status=HeadlessStatus.UNAUTHORIZED,
            workspace=str(resolved_ws),
            error_category="provider_approval_required",
            error=str(exc),
            shell_authorized=allow_shell,
        )
    except ProviderApprovalMismatch as exc:
        result = HeadlessResult(
            task_id=admitted_id,
            status=HeadlessStatus.UNAUTHORIZED,
            workspace=str(resolved_ws),
            error_category="provider_approval_mismatch",
            error=str(exc),
            shell_authorized=allow_shell,
        )
    except (OSError, ValueError) as exc:
        result = HeadlessResult(
            task_id=admitted_id,
            status=HeadlessStatus.RUNTIME_ERROR,
            workspace=str(resolved_ws),
            error_category="configuration_error",
            error=str(exc),
            shell_authorized=allow_shell,
        )
    _emit(result, as_json=emit_json, report=report)
    raise typer.Exit(_exit_code(result))
