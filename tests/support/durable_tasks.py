"""Fresh offline task hosts and native process-death barriers."""

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from cairn.agent.assembly import build_agent
from cairn.agent.budget import RunBudget
from cairn.agent.context.git import RepoContextProvider
from cairn.agent.harness import Agent
from cairn.agent.models.client import LLMClient
from cairn.agent.models.messages import LLMResponse, Message, ToolCall
from cairn.agent.permissions import SessionPermissionHandler
from cairn.agent.session.access import open_store
from cairn.agent.session.durable import DurableSession
from cairn.agent.session.host import observe_workspace
from cairn.agent.session.models import HostResolution, SessionBinding
from cairn.agent.session.store import SessionStore
from cairn.agent.turn.events import EventHandler
from cairn.agent.workspace.workspace import Workspace
from cairn.development.execution.models import TaskSpec
from cairn.development.execution.runner import CodingTaskRunner
from tests.support.git import initialize_repository
from tests.support.runtime import SequenceLLM
from tests.support.sessions import SESSION, UNVERIFIED_RUNTIME

SPEC = TaskSpec(task_id="durable-task", prompt="Update tracked.txt")
BUDGET = RunBudget(max_steps=3)


def edit_response() -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(
                id="edit-1",
                name="edit_file",
                arguments={
                    "path": "tracked.txt",
                    "old_text": "before\n",
                    "new_text": "after\n",
                },
            )
        ]
    )


@dataclass
class DurableTaskHost:
    store: SessionStore
    session: DurableSession
    runner: CodingTaskRunner
    agent: Agent
    workspace: Workspace


def make_host(
    root: Path,
    llm: LLMClient | None = None,
    *,
    event_handler: EventHandler | None = None,
    reopen: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> DurableTaskHost:
    work = root / "work"
    if not work.exists():
        work.mkdir()
        initialize_repository(work)
    workspace = Workspace(work)
    client = (
        llm
        if llm is not None
        else SequenceLLM([edit_response(), LLMResponse(content="done")])
    )
    permissions = SessionPermissionHandler()
    agent = build_agent(
        workspace=workspace,
        llm=client,
        permission_handler=permissions,
        event_handler=event_handler,
        tracer=None,
    )
    api = open_store(
        root / "store",
        workspace,
        sensitive_values=sensitive_values,
        allow_unverified=UNVERIFIED_RUNTIME,
    )

    def observe() -> SessionBinding:
        return SessionBinding(
            session_id=SESSION,
            workspace=observe_workspace(workspace, frozenset()),
            provider=None,
            observed_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

    binding = observe()
    if reopen:
        session = DurableSession.reopen(
            api, SESSION, agent=agent, binding=binding, observe_binding=observe
        )
    else:
        session = DurableSession.create(
            api, agent=agent, binding=binding, observe_binding=observe
        )
    runner = CodingTaskRunner(
        workspace=workspace,
        llm=client,
        budget=BUDGET,
        permission_handler=permissions,
        event_handler=event_handler,
    )
    return DurableTaskHost(api, session, runner, agent, workspace)


def reconcile_fresh(host: DurableTaskHost) -> None:
    report = host.session.inspect()
    binding = SessionBinding(
        session_id=SESSION,
        workspace=observe_workspace(host.workspace, frozenset()),
        provider=None,
        observed_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    host.session.reconcile(
        report,
        host_resolution=HostResolution(
            session_id=SESSION,
            turn_id=report.unresolved.intent.turn_id if report.unresolved else None,
            actor="offline-host",
            resolved_at_utc=binding.observed_at_utc,
            binding=binding,
        ),
    )


def crash_task_writer(root_text: str, window: str, barrier: Connection) -> None:
    def stop() -> None:
        barrier.send(window)
        barrier.recv()

    class CrashLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            self.calls += 1
            if (window == "model" and self.calls == 1) or (
                window == "effect" and self.calls == 2
            ):
                stop()
            return edit_response() if self.calls == 1 else LLMResponse(content="done")

    host = make_host(Path(root_text), CrashLLM())
    api = host.store
    monkeypatch = pytest.MonkeyPatch()
    if window == "before_admission":
        stop()
    if window in ("admitted", "fenced"):
        method = "create_task" if window == "admitted" else "fence_tool"
        original = getattr(api, method)

        def after_write(*args: Any, **kwargs: Any) -> object:
            result = original(*args, **kwargs)
            stop()
            return result

        monkeypatch.setattr(api, method, after_write)
    if window in ("checkpoint", "inspection_done", "result_commit"):
        method = "commit_task_turn" if window == "checkpoint" else "record_task_result"
        original_result = getattr(api, method)

        def before_write(*args: Any, **kwargs: Any) -> object:
            if window == "result_commit":

                def unknown(conn: sqlite3.Connection) -> None:
                    conn.commit()
                    stop()

                monkeypatch.setattr(api, "_commit", unknown)
            else:
                stop()
            return original_result(*args, **kwargs)

        monkeypatch.setattr(api, method, before_write)
    if window == "inspecting":

        async def inspect(
            self: RepoContextProvider, *, secret_env_keys: frozenset[str] = frozenset()
        ) -> None:
            stop()

        monkeypatch.setattr(RepoContextProvider, "inspect_evidence", inspect)
    try:
        asyncio.run(host.runner.run(SPEC, durable_session=host.session))
    finally:
        monkeypatch.undo()
        host.session.close()
