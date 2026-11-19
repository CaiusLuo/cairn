"""Open a SessionStore against the shared PostgreSQL database."""

from cairn.agent.session.store import SessionStore
from cairn.shared.db import Database


def open_store(
    database: Database,
    *,
    inspect: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> SessionStore:
    return SessionStore(
        database,
        mode="inspect" if inspect else "writer",
        sensitive_values=sensitive_values,
    )
