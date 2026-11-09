"""Offline hosts explicitly observe and approve DurableSession bindings."""

from datetime import UTC, datetime
from pathlib import Path

from cairn.agent.harness import Agent
from cairn.agent.session.durable import DurableSession
from cairn.agent.session.models import HostResolution, RecoveryReport
from cairn.agent.session.store import SessionStore
from tests.support.sessions import binding, store


def create_session(root: Path, agent: Agent) -> tuple[SessionStore, DurableSession]:
    api = store(root)
    return api, DurableSession.create(api, agent=agent, binding=binding(root))


def resolution(report: RecoveryReport) -> HostResolution:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return HostResolution(
        session_id=report.revision.session_id,
        turn_id=report.unresolved.intent.turn_id if report.unresolved else None,
        actor="offline-host",
        resolved_at_utc=now,
        binding=report.binding.model_copy(update={"observed_at_utc": now}),
    )
