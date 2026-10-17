"""Pin a qualified SQLite runtime for store-level tests.

Project Memory and the session store both refuse an unqualified SQLite runtime
before any filesystem or database effect (ADR 0002). Tests that exercise store
behaviour rather than admission must not depend on whichever SQLite the host
happens to link, or they pass on CI and fail on a developer machine.

This helper does **not** restate the version rule. It substitutes only the
*linked version*, so the real ``sqlite_wal_fix_supported`` and
``wal_admission`` keep making the decision. A partial copy of the rule here
(``>= 3.51.3``) would silently omit the 3.50.7+ and 3.44.6+ backports and drift
from the policy it is meant to mirror.
"""

from __future__ import annotations

import sqlite3
from typing import cast

import pytest

#: A version inside the verified set (3.51.3+, 3.50.7+ or 3.44.6+). The policy
#: decides whether it qualifies; this is only the value presented to it.
QUALIFIED_SQLITE = (3, 53, 1)


def pin_qualified_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Present a qualified version to the real policy.

    Only ``sqlite3.sqlite_version_info`` is replaced, which is the single input
    both policy functions default to. Their logic is untouched.
    """
    monkeypatch.setattr(sqlite3, "sqlite_version_info", QUALIFIED_SQLITE)


def pin_case_version(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
) -> None:
    """Present an arbitrary version, for tests that assert a specific verdict."""
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)


def pin_worker_runtime() -> None:
    """Present the qualified version inside a spawned worker.

    A spawn child re-imports sqlite3, so the parent's monkeypatch cannot reach it.
    The attribute is typed Final, which is why this goes through the module dict
    rather than an assignment; the policy logic is untouched.
    """
    module = cast("dict[str, object]", vars(sqlite3))
    module["sqlite_version_info"] = QUALIFIED_SQLITE
