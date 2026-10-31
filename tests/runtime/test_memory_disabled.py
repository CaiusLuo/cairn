from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cairn.infra.agent_memory.store import AgentMemoryStore
from cairn.runtime.agent_memory.service import PersonalMemoryService
from cairn.runtime.agent_memory.tools import (
    MemoryCorrectTool,
    MemoryForgetTool,
    MemoryRememberTool,
    MemorySearchTool,
)


def test_disabled_memory_tools_fail_closed_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_AGENT_MEMORY", "off")
    store = AgentMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md("\n- secret-memory-must-not-leak\n")
    service = PersonalMemoryService(store)
    assert service.enabled is False
    for tool, args in (
        (MemorySearchTool(service), {"query": "secret"}),
        (MemoryRememberTool(service), {"text": "new"}),
        (MemoryForgetTool(service), {"query": "secret"}),
        (MemoryCorrectTool(service), {"old": "secret", "new": "other"}),
    ):
        result = asyncio.run(tool.execute(args))
        assert result.exit_code != 0
        assert "disabled" in result.stderr
        assert "secret-memory-must-not-leak" not in result.stdout


def test_disabled_service_search_returns_no_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_AGENT_MEMORY", "off")
    store = AgentMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md("\n- hidden-fact\n")
    service = PersonalMemoryService(store)
    assert service.enabled is False
    assert service.search_facts("hidden") == []
    assert service.bootstrap().memory_md == ""
