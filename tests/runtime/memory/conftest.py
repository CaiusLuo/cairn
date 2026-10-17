"""Pin a qualified SQLite runtime for the runtime-memory suite.

These tests build real MemoryStore instances to exercise retrieval, provenance
and evidence; admission itself is covered in tests/infra/memory/test_wal_policy.py.
"""

import pytest

from tests.support.sqlite_runtime import pin_qualified_runtime


@pytest.fixture(autouse=True)
def qualified_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_qualified_runtime(monkeypatch)
