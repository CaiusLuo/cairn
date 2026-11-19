"""Shared test isolation for host-private personal state.

Tests must never read or append the operator's real personal AGENT.md, daily
journal, schedule store or WeChat state. Subprocesses inherit these variables.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def clean_test_databases() -> Iterator[None]:
    from tests.support.pg import reset_databases

    yield
    reset_databases()


@pytest.fixture(autouse=True)
def isolate_host_private_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    from tests.support.pg import namespace_for, test_database_url

    root = tmp_path_factory.mktemp("host-private-state")
    state = root / "xdg-state"
    # A configuration that does not inject its own Database must still never
    # reach the operator's cairn database.
    monkeypatch.setenv("CAIRN_DATABASE_URL", test_database_url())
    monkeypatch.setenv("CAIRN_DATABASE_NAMESPACE", namespace_for(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
    monkeypatch.setenv("CAIRN_SCHEDULE_DIR", str(root / "schedule"))
    monkeypatch.setenv("CAIRN_WECHAT_STATE_DIR", str(root / "wechat"))
    yield
