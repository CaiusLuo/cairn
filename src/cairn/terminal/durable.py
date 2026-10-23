"""CLI storage, trusted bindings and explicit human recovery presentation."""

import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import typer

from cairn.infra.sessions.host import observe_workspace
from cairn.infra.sessions.models import (
    HostResolution,
    ProviderReference,
    RecoveryReport,
    SessionBinding,
    SessionStoreError,
    SessionSummary,
    same_workspace,
)
from cairn.infra.sessions.store import SessionStore
from cairn.infra.tools.bash import (
    build_command_env,
    resolve_tmpdir,
    resolve_uv_cache_dir,
)
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.provider_runtime import ProviderRuntime


@dataclass(frozen=True)
class DurableOptions:
    operation: Literal["new", "reopen", "reconcile"]
    session_id: str | None = None
    directory: Path | None = None
    additional_roots: tuple[Path, ...] = ()


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def open_store(
    directory: Path | None,
    workspace: Workspace,
    *,
    inspect: bool = False,
    secret_env_keys: frozenset[str] = frozenset(),
    sensitive_values: tuple[str, ...] = (),
    additional_roots: tuple[Path, ...] = (),
    allow_unverified: bool = False,
) -> SessionStore:
    # A store inside a scratch or cache directory can be deleted under a live
    # session, so those roots are refused regardless of the shell's sandboxing.
    # Match the child environment rather than raw host variables, because
    # credential filtering may have removed some of them.
    env = build_command_env(os.environ, secret_env_keys)
    roots = [workspace.root, *additional_roots]
    for path in (resolve_tmpdir(env), resolve_uv_cache_dir(env)):
        if path is not None:
            roots.append(path)
    return SessionStore(
        directory or Path.home() / ".local/state/cairn/sessions",
        mode="inspect" if inspect else "writer",
        forbidden_roots=tuple(roots),
        sensitive_values=sensitive_values,
        allow_unverified=allow_unverified,
    )


def list_sessions(directory: Path | None, additional_roots: tuple[Path, ...]) -> None:
    """Print the Sessions that can be reopened, most recently updated first.

    Read-only: this opens the store in inspection mode and reports what it holds,
    so looking for a conversation cannot change one.
    """
    store = open_store(
        directory,
        Workspace(Path.cwd()),
        inspect=True,
        additional_roots=additional_roots,
    )
    try:
        sessions = store.list_sessions()
    finally:
        store.close()
    if not sessions:
        typer.echo("No stored sessions.")
        return
    typer.echo(f"{'ID':<8} {'STATE':<19} {'UPDATED':<21} TITLE")
    for summary in sessions:
        typer.echo(
            f"{summary.short_id:<8} {summary.state:<19} "
            f"{summary.updated_at_utc:<21} {_display_title(summary)}"
        )
    typer.echo("")
    typer.echo("Reopen one with: cairn session reopen <ID>")


def _display_title(summary: SessionSummary) -> str:
    if summary.title:
        return summary.title
    return "(untitled; the title is written after the first reply)"


def show_report(report: RecoveryReport) -> None:
    typer.echo(f"Session: {report.revision.session_id}")
    typer.echo(f"Last committed checkpoint: {report.receipt.revision.head_seq}")
    typer.echo(f"Recovery: {report.status}; revision {report.revision.version}")
    turn = report.unresolved
    if turn is not None:
        fences = turn.diagnostics.fences
        typer.echo(
            f"Unfinished Turn: {turn.intent.turn_id}; effect fences: {len(fences)}"
        )
        # UUIDs/ordinals only: stored tool names/call IDs can contain user data.
        for fence in fences[-8:]:
            typer.echo(f"  Effect fence #{fence.ordinal}")
        typer.echo(
            "External effects may already have occurred. Never replay this turn."
        )
        typer.echo(
            "Inspect effects and surviving work before abandoning without replay."
        )
    else:
        typer.echo("Unfinished Turn: none")


def inspect_session(
    session_id: str, directory: Path | None, additional_roots: tuple[Path, ...]
) -> None:
    store = open_store(
        directory,
        Workspace(Path.cwd()),
        inspect=True,
        additional_roots=additional_roots,
    )
    try:
        show_report(store.inspect(store.resolve_session_id(session_id)))
    finally:
        store.close()


def interactive_terminal() -> bool:
    return sys.stdin.isatty()


def confirm_resolution(report: RecoveryReport, binding: SessionBinding) -> bool:
    """A terminal human approves this exact report; EOF/default is never consent."""
    if not interactive_terminal():
        typer.echo("Recovery confirmation requires an interactive terminal.")
        return False
    if report.binding.provider != binding.provider:
        typer.echo("Current approved provider binding differs from the stored binding.")
    if report.binding.workspace.git != binding.workspace.git:
        typer.echo("Current Git HEAD/branch differs from the stored binding.")
    turn_id = report.unresolved.intent.turn_id if report.unresolved else "none"
    token = (
        f"abandon_without_replay {report.revision.session_id} "
        f"{turn_id} {report.revision.version}"
    )
    typer.echo("Acknowledge the current Git/provider binding and retained checkpoint.")
    typer.echo(f"To confirm, type exactly: {token}")
    try:
        answer: object = typer.prompt("Confirmation", default="", show_default=False)
        return isinstance(answer, str) and answer == token
    except (EOFError, KeyboardInterrupt, typer.Abort):
        return False


def attach_session(
    options: DurableOptions,
    agent: Agent,
    workspace: Workspace,
    runtime: ProviderRuntime,
    *,
    max_steps: int,
    secret_env_keys: frozenset[str],
    sensitive_values: tuple[str, ...],
) -> tuple[DurableSession, Callable[[], SessionBinding]]:
    session_id = options.session_id or str(uuid4())
    group = runtime.model_manager.current_model()
    # Hash routing/config facts, never credentials. Stored fingerprints are
    # comparison evidence only; provider approval happened in this host lifecycle.
    config = runtime.provider.config
    provider = ProviderReference(
        provider=runtime.provider.name,
        model_group=group.name,
        model_ids=group.model_ids,
        config_digest=hashlib.sha256(
            json.dumps(
                [config.base_url, config.api_key_env, group.name, group.model_ids]
            ).encode()
        ).hexdigest(),
        system_prompt_digest=hashlib.sha256(agent.system_prompt.encode()).hexdigest(),
        context_budget=agent.context_builder.budget.max_tokens,
        output_reserve=agent.context_builder.budget.response_tokens,
        max_steps=max_steps,
    )

    def observe() -> SessionBinding:
        return SessionBinding(
            session_id=session_id,
            workspace=observe_workspace(workspace, secret_env_keys),
            provider=provider,
            observed_at_utc=now(),
        )

    binding = observe()
    # Inspect and reject identity changes before opening a writer.
    if options.operation != "new":
        reader = open_store(
            options.directory,
            workspace,
            inspect=True,
            secret_env_keys=secret_env_keys,
            sensitive_values=sensitive_values,
            additional_roots=options.additional_roots,
        )
        try:
            report = reader.inspect(session_id)
            if not same_workspace(report.binding, binding):
                raise SessionStoreError("workspace_identity_mismatch")
        finally:
            reader.close()
    store = open_store(
        options.directory,
        workspace,
        secret_env_keys=secret_env_keys,
        sensitive_values=sensitive_values,
        additional_roots=options.additional_roots,
    )
    try:
        if options.operation == "new":
            session = DurableSession.create(
                store, agent=agent, binding=binding, observe_binding=observe
            )
        else:
            # A caller may type the abbreviated identifier `session list` shows;
            # resolving it here keeps the store the only authority on which
            # Session that is, and refuses an ambiguous prefix before any state
            # changes.
            session = DurableSession.reopen(
                store,
                store.resolve_session_id(session_id),
                agent=agent,
                binding=binding,
                observe_binding=observe,
            )
        return session, observe
    except BaseException:
        store.close()
        raise


def reconcile_session(
    session: DurableSession,
    observe: Callable[[], SessionBinding],
    *,
    allow_unfinished: bool,
) -> bool:
    report = session.inspect()
    show_report(report)
    if (report.unresolved is not None or report.local_fault) and not allow_unfinished:
        typer.echo(
            "Recovery required. Use cairn session reconcile SESSION_ID explicitly."
        )
        return False
    binding = observe()
    if not same_workspace(report.binding, binding):
        raise SessionStoreError("workspace_identity_mismatch")
    if not confirm_resolution(report, binding):
        typer.echo("Session remains suspended; nothing was approved or replayed.")
        return False
    current = observe()
    if (
        current.model_copy(update={"observed_at_utc": binding.observed_at_utc})
        != binding
    ):
        raise SessionStoreError("binding_changed_during_confirmation")
    session.reconcile(
        report,
        host_resolution=HostResolution(
            session_id=report.revision.session_id,
            turn_id=report.unresolved.intent.turn_id if report.unresolved else None,
            actor="cli-human",
            resolved_at_utc=now(),
            binding=current,
        ),
    )
    typer.echo("Reconciled without replay.")
    return True
