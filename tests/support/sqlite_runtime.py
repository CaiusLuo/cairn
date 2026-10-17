"""Pin a qualified SQLite runtime for store-level tests.

Project Memory and the session store both refuse an unqualified SQLite runtime
before any filesystem or database effect (ADR 0002). Tests that exercise store
behaviour rather than the admission gate must not depend on whichever SQLite the
host happens to link, or they pass on CI and fail on a developer machine.

Only ``sqlite_wal_fix_supported`` is patched: ``wal_admission`` is derived from
it, so the policy keeps one decision owner. Admission itself is covered
explicitly in ``tests/infra/memory/test_wal_policy.py``.
"""

from __future__ import annotations

import pytest

#: A runtime inside the verified set (3.51.3+, 3.50.7+ or 3.44.6+).
type SqliteVersion = tuple[int, int, int]

#: A runtime inside the verified set (3.51.3+, 3.50.7+ or 3.44.6+).
QUALIFIED_SQLITE: SqliteVersion = (3, 53, 1)


def pin_qualified_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behave as if the linked sqlite3 were a verified version."""
    import cairn.infra.sqlite_policy as policy

    pinned: SqliteVersion = QUALIFIED_SQLITE

    def pinned_supported(version: tuple[int, int, int] | None = None) -> bool:
        return (pinned if version is None else version) >= (3, 51, 3)

    monkeypatch.setattr(policy, "sqlite_wal_fix_supported", pinned_supported)
