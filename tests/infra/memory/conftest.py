"""Pin a qualified SQLite runtime for the MemoryStore suite.

MemoryStore refuses an unqualified runtime before any effect (ADR 0002, #63).
These tests exercise store behaviour, so they pin the predicate; the gate itself
is covered in ``test_wal_policy.py``.
"""

import pytest

from tests.support.sqlite_runtime import pin_qualified_runtime


@pytest.fixture(autouse=True)
def qualified_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_qualified_runtime(monkeypatch)
