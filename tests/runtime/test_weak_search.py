from __future__ import annotations

from pathlib import Path

from cairn.infra.agent_memory.store import AgentMemoryStore
from cairn.runtime.agent_memory.service import PersonalMemoryService


def test_weak_match_keeps_identity_candidate(tmp_path: Path) -> None:
    store = AgentMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md("\n- 姓名: 张三\n- 我是软件工程师\n")
    service = PersonalMemoryService(store)
    matches = service.search_facts("我是谁")
    text = "\n".join(fact for _, _, fact in matches)
    assert "张三" in text
    assert "软件工程师" in text
    assert any("long-term candidate" in source for source, _, _ in matches)
