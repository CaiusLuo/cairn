from __future__ import annotations

import asyncio
from pathlib import Path

from cairn.infra.agent_memory.store import AgentMemoryStore
from cairn.runtime.agent_memory.service import PersonalMemoryService
from cairn.runtime.agent_memory.tools import (
    MemoryCorrectTool,
    MemoryForgetTool,
    MemoryRememberTool,
    MemorySearchTool,
)


def test_memory_tools_remember_search_correct_forget(tmp_path: Path) -> None:
    service = PersonalMemoryService(AgentMemoryStore(tmp_path / "agent"))
    remember = MemoryRememberTool(service)
    search = MemorySearchTool(service)
    correct = MemoryCorrectTool(service)
    forget = MemoryForgetTool(service)

    assert (
        asyncio.run(remember.execute({"text": "prefer concise answers"})).exit_code == 0
    )
    found = asyncio.run(search.execute({"query": "concise"}))
    assert "prefer concise answers" in found.stdout

    assert (
        asyncio.run(correct.execute({"old": "concise", "new": "short"})).exit_code == 0
    )
    assert "prefer concise answers" not in service.store.read_memory_md()
    assert "short" in service.store.read_memory_md()

    assert asyncio.run(forget.execute({"query": "short"})).exit_code == 0
    assert "short" not in service.store.read_memory_md()
