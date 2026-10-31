"""Shared test isolation for host-private personal state.

Tests must never read or append the operator's real personal AGENT.md, daily
journal, schedule store or WeChat state. Subprocesses inherit these variables.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def isolate_host_private_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    root = tmp_path_factory.mktemp("host-private-state")
    state = root / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("CAIRN_AGENT_HOME", str(root / "agent"))
    monkeypatch.setenv("CAIRN_SCHEDULE_DIR", str(root / "schedule"))
    monkeypatch.setenv("CAIRN_WECHAT_STATE_DIR", str(root / "wechat"))
    yield
