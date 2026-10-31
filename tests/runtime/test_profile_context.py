from __future__ import annotations

import json

from cairn.core.agent_memory import PersonalMemoryContext


def test_profile_survives_budget_pressure_and_chinese_json() -> None:
    context = PersonalMemoryContext(
        profile="姓名：张三\n偏好：中文",
        agent_md="## Preferences\n- 姓名：张三\n" + ("A" * 4000),
        memory_md="MEMORY.md:" + ("记忆" * 2000),
        daily=(("2026-11-01", "## 09:00\n- text: 今天讨论部署"),),
        max_chars=1800,
    )
    rendered = context.render()
    assert len(rendered) <= 1800
    payload = json.loads(rendered.split("\n", 1)[1])
    profile = payload["personal_agent_memory"]["profile"]
    assert "张三" in profile


def test_daily_shrinking_drops_complete_entries_not_half_an_entry() -> None:
    body = (
        "## 09:00\n- text: first\n\n## 10:00\n- text: second\n\n## 11:00\n- text: third"
    )
    context = PersonalMemoryContext(
        daily=(("2026-11-01", body),),
        max_chars=1200,
    )
    rendered = context.render()
    payload = json.loads(rendered.split("\n", 1)[1])
    daily = payload["personal_agent_memory"]["daily"][0]["text"]
    assert daily == "" or daily.startswith("## ")
    if daily:
        assert daily.count("## ") >= 1
        assert daily.rstrip().endswith(("first", "second", "third"))


def test_existing_long_term_memory_supplies_profile_when_agent_md_is_template(
    tmp_path,
) -> None:
    from cairn.infra.agent_memory.store import AgentMemoryStore
    from cairn.runtime.agent_memory.service import PersonalMemoryService

    store = AgentMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md("\n- 姓名：李四\n- 喜欢中文\n")
    service = PersonalMemoryService(store)
    context = service.bootstrap()
    assert "李四" in context.profile
    assert "李四" in service.render_context()


def test_daily_selection_prefers_newest_entries_and_keeps_reading_order() -> None:
    body = "\n\n".join(
        f"## {hour:02d}:00\n- text: item-{hour:02d} " + ("长" * 400)
        for hour in range(20)
    )
    context = PersonalMemoryContext(
        daily=(("2026-11-01", body),),
        max_chars=1600,
    )
    payload = json.loads(context.render().split("\n", 1)[1])
    daily = payload["personal_agent_memory"]["daily"]
    assert daily
    texts = "\n".join(item["text"] for item in daily)
    assert "item-19" in texts
    assert "item-00" not in texts


def test_daily_selection_does_not_depend_on_caller_day_order() -> None:
    first = "## 09:00\n- text: old"
    second = "## 09:00\n- text: new"
    context = PersonalMemoryContext(
        daily=(("2026-11-02", second), ("2026-11-01", first)),
        max_chars=1200,
    )
    payload = json.loads(context.render().split("\n", 1)[1])
    daily = payload["personal_agent_memory"]["daily"]
    assert daily
    assert daily[0]["text"].endswith("old")


def test_realistic_large_daily_and_long_term_entries_are_not_all_dropped() -> None:
    daily = (
        (
            "2026-11-01",
            "\n\n".join(f"## {i:02d}:00\n- text: " + ("长" * 300) for i in range(100)),
        ),
        (
            "2026-11-02",
            "\n\n".join(
                f"## {i:02d}:00\n- text: " + ("记忆" * 150) for i in range(100)
            ),
        ),
    )
    memory = "# Memory\n- [legacy] Name: Alex Q.\n- [legacy] Prefers concise replies.\n"
    context = PersonalMemoryContext(
        profile="", agent_md="# Agent", memory_md=memory, daily=daily, max_chars=12000
    )
    rendered = context.render()
    payload = json.loads(rendered.split("\n", 1)[1])
    data = payload["personal_agent_memory"]
    assert "Alex Q." in data["memory_md"]
    assert data["daily"]
    assert len(rendered) <= 12000


def test_profile_does_not_clear_other_long_term_entries() -> None:
    memory = "# Memory\n- [legacy] Name: Alex Q.\n- [legacy] Prefers concise replies.\n"
    context = PersonalMemoryContext(
        profile="Alex Q.",
        agent_md="# Agent",
        memory_md=memory,
        daily=(("2026-11-01", "## 09:00\n- text: today"),),
        max_chars=1200,
    )
    payload = json.loads(context.render().split("\n", 1)[1])
    assert "Alex Q." in payload["personal_agent_memory"]["profile"]
    assert "concise" in payload["personal_agent_memory"]["memory_md"]
