"""Offline hosts explicitly observe and approve DurableSession bindings."""

from datetime import UTC, datetime
from pathlib import Path

from cairn.infra.sessions.models import HostResolution, RecoveryReport
from cairn.infra.sessions.store import SessionStore
from cairn.runtime.agent import Agent
from cairn.runtime.durable_session import DurableSession
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
