from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from cairn.agent.models.messages import LLMResponse
from cairn.agent.permissions import SessionPermissionHandler
from cairn.agent.session.durable import DurableSession
from cairn.agent.session.host import observe_workspace
from cairn.agent.session.models import HostResolution, SessionBinding
from cairn.agent.session.store import SessionStore
from cairn.agent.workspace.workspace import Workspace
from cairn.development.execution import CodingTaskRunner
from cairn.development.execution.models import TaskSpec
from tests.support.durable_tasks import BUDGET
from tests.support.git import initialize_repository
from tests.support.runtime import SequenceLLM


def test_runner_durable_factory_restores_messages() -> None:
    with TemporaryDirectory(prefix="cairn-wechat-durable-", dir=Path.home()) as d:
        base = Path(d)
        root = base / "data"
        root.mkdir(mode=0o700)
        ws_root = base / "ws"
        ws_root.mkdir()
        initialize_repository(ws_root)
        workspace = Workspace(ws_root)
        llm = SequenceLLM(
            [LLMResponse(content="first answer"), LLMResponse(content="second answer")]
        )
        session_id = str(uuid4())

        stores: list[SessionStore] = []
        sessions = []

        def factory(agent):  # type: ignore[no-untyped-def]
            store = (
                stores[0]
                if stores
                else SessionStore(root, forbidden_roots=(workspace.root,))
            )
            if not stores:
                stores.append(store)
            binding = SessionBinding(
                session_id=session_id,
                workspace=observe_workspace(workspace, frozenset()),
                provider=None,
                observed_at_utc=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            try:
                store.inspect(session_id)
            except Exception:
                session = DurableSession.create(
                    store, agent=agent, binding=binding, observe_binding=lambda: binding
                )
                sessions.append(session)
                return session
            session = DurableSession.reopen(
                store,
                session_id,
                agent=agent,
                binding=binding,
                observe_binding=lambda: binding,
            )
            report = session.inspect()
            session.reconcile(
                report,
                host_resolution=HostResolution(
                    session_id=session_id,
                    turn_id=None,
                    actor="wechat-test",
                    resolved_at_utc=datetime.now(UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    binding=binding,
                ),
            )
            sessions.append(session)
            return session

        runner = CodingTaskRunner(
            workspace=workspace,
            llm=llm,
            budget=BUDGET,
            permission_handler=SessionPermissionHandler(),
            durable_session_factory=factory,
        )

        async def flow() -> None:
            first = await runner.run(TaskSpec(task_id="one", prompt="first prompt"))
            assert first.status.value == "completed"
            second = await runner.run(TaskSpec(task_id="two", prompt="second prompt"))
            assert second.status.value == "completed"

        asyncio.run(flow())
        assert len(llm.calls) == 2
        contents = [message.content for message in llm.calls[1][0]]
        assert "first prompt" in contents and "first answer" in contents
