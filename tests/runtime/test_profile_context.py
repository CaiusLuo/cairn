from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from cairn.agent.models.messages import LLMResponse
from cairn.memory.models import PersonalMemoryContext
from cairn.memory.service import PersonalMemoryService
from cairn.memory.store import PersonalMemoryStore


class FakeLLM:
    def __init__(self, content: str | None = "[MEMORY.md] Name: Alex") -> None:
        self.content = content
        self.calls = 0
        self.messages: list[object] = []

    async def generate(self, messages, tools=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.messages = messages
        return LLMResponse(content=self.content)


def test_profile_survives_budget_pressure_and_chinese_json() -> None:
    context = PersonalMemoryContext(
        profile="姓名:张三\n偏好:中文",
        agent_md="## Preferences\n- 姓名:张三\n" + ("A" * 4000),
        memory_md="MEMORY.md:" + ("记忆" * 2000),
        daily=(("2026-11-01", "## 09:00\n- text: 今天讨论部署"),),
        max_chars=1800,
    )
    rendered = context.render()
    assert len(rendered) <= 1800
    payload = json.loads(rendered.split("\n", 1)[1])
    assert "张三" in payload["personal_agent_memory"]["profile"]


def test_daily_shrinking_drops_complete_entries_not_half_an_entry() -> None:
    body = (
        "## 09:00\n- text: first\n\n## 10:00\n- text: second\n\n## 11:00\n- text: third"
    )
    context = PersonalMemoryContext(daily=(("2026-11-01", body),), max_chars=1200)
    payload = json.loads(context.render().split("\n", 1)[1])
    daily = payload["personal_agent_memory"]["daily"][0]["text"]
    assert daily == "" or daily.startswith("## ")
    if daily:
        assert daily.rstrip().endswith(("first", "second", "third"))


def test_existing_long_term_memory_supplies_profile_when_agent_md_is_template(
    tmp_path: Path,
) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md("\n- 姓名: 李四\n- 喜欢中文\n")
    service = PersonalMemoryService(store)
    assert "李四" in service.bootstrap().profile


def test_daily_selection_prefers_newest_entries_and_keeps_reading_order() -> None:
    body = "\n\n".join(
        f"## {hour:02d}:00\n- text: item-{hour:02d} " + ("长" * 400)
        for hour in range(20)
    )
    context = PersonalMemoryContext(daily=(("2026-11-01", body),), max_chars=1600)
    daily = json.loads(context.render().split("\n", 1)[1])["personal_agent_memory"][
        "daily"
    ]
    texts = "\n".join(item["text"] for item in daily)
    assert "item-19" in texts
    assert "item-00" not in texts


def test_daily_selection_does_not_depend_on_caller_day_order() -> None:
    context = PersonalMemoryContext(
        daily=(
            ("2026-11-02", "## 09:00\n- text: new"),
            ("2026-11-01", "## 09:00\n- text: old"),
        ),
        max_chars=1200,
    )
    daily = json.loads(context.render().split("\n", 1)[1])["personal_agent_memory"][
        "daily"
    ]
    assert daily and daily[0]["text"].endswith("old")


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
    memory = "# Memory\n- [legacy] Name: Alex\n- [legacy] Prefers concise replies.\n"
    context = PersonalMemoryContext(
        profile="", agent_md="# Agent", memory_md=memory, daily=daily, max_chars=12000
    )
    rendered = context.render()
    data = json.loads(rendered.split("\n", 1)[1])["personal_agent_memory"]
    assert "Alex" in data["memory_md"]
    assert data["daily"]
    assert len(rendered) <= 12000


def test_profile_does_not_clear_other_long_term_entries() -> None:
    memory = "# Memory\n- [legacy] Name: Alex\n- [legacy] Prefers concise replies.\n"
    context = PersonalMemoryContext(
        profile="Alex",
        agent_md="# Agent",
        memory_md=memory,
        daily=(("2026-11-01", "## 09:00\n- text: today"),),
        max_chars=1200,
    )
    data = json.loads(context.render().split("\n", 1)[1])["personal_agent_memory"]
    assert "Alex" in data["profile"]
    assert "concise" in data["memory_md"]


def test_long_term_section_facts_are_selected_individually() -> None:
    facts = "\n".join(f"- [legacy] fact-{i:03d} " + ("记" * 80) for i in range(100))
    context = PersonalMemoryContext(
        memory_md=f"## Saved\n{facts}\n",
        daily=(("2026-11-01", "## 09:00\n- text: recent"),),
        max_chars=3000,
    )
    memory = json.loads(context.render().split("\n", 1)[1])["personal_agent_memory"][
        "memory_md"
    ]
    assert "fact-000" in memory
    assert memory.count("fact-") >= 2


def test_profile_extraction_redacts_and_preserves_source(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md(
        "\n- [legacy] Name: Alex\n- [legacy] secret: fake-provider-key\n"
    )
    service = PersonalMemoryService(store, secret_values=("fake-provider-key",))
    llm = FakeLLM()
    assert asyncio.run(service.ensure_profile(llm)) is None
    assert llm.calls == 1
    sent = "\n".join(str(message.content) for message in llm.messages)  # type: ignore[attr-defined]
    assert "fake-provider-key" not in sent
    profile = store.read_profile()
    assert (
        "Alex" in profile and "[file: MEMORY.md]" in profile and "[legacy]" in profile
    )
    assert "secret: fake-provider-key" in store.read_memory_md()


def test_default_templates_do_not_trigger_an_extra_request(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    llm = FakeLLM()
    assert asyncio.run(PersonalMemoryService(store).ensure_profile(llm)) is None
    assert llm.calls == 0


def test_fabricated_profile_fact_is_rejected(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md("\n- [legacy] Name: Alex\n")
    error = asyncio.run(
        PersonalMemoryService(store).ensure_profile(
            FakeLLM("[MEMORY.md] Name: Invented")
        )
    )
    assert error is not None and "verifiable" in error
    assert "Invented" not in store.read_profile()


def test_agent_source_is_not_misreported_as_memory(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.write_agent_md("# Personal\n\n## Preferences\n- prefers tabs\n")
    assert (
        asyncio.run(
            PersonalMemoryService(store).ensure_profile(
                FakeLLM("[AGENT.md] prefers tabs")
            )
        )
        is None
    )
    assert "[file: AGENT.md] - prefers tabs" in store.read_profile()


def test_profile_extraction_failure_is_redacted_and_does_not_block(
    tmp_path: Path,
) -> None:
    class Broken:
        async def generate(self, messages, tools=None):  # type: ignore[no-untyped-def]
            raise RuntimeError("provider fake-provider-key failed")

    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md("\n- [legacy] fact\n")
    service = PersonalMemoryService(store, secret_values=("fake-provider-key",))
    error = asyncio.run(service.ensure_profile(Broken()))
    assert error is not None and "fake-provider-key" not in error
    assert "[REDACTED]" in error
    assert "fact" in store.read_memory_md()


def test_profile_correction_and_deletion_cover_profile(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.write_profile(
        "# Personal profile\n- [file: MEMORY.md] Name: Alex\n- [file: MEMORY.md] likes tea\n"
    )
    service = PersonalMemoryService(store)
    assert service.correct_fact("Name: Alex", "Name: Bob") is True
    assert "Bob" in store.read_profile()
    assert service.forget_fact("likes tea") is True
    assert "likes tea" not in store.read_profile()


def test_migrated_profile_is_corrected_and_forgotten_end_to_end(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md("\n- [legacy] Name: Alex\n")
    service = PersonalMemoryService(store)
    assert (
        asyncio.run(service.ensure_profile(FakeLLM("[MEMORY.md] Name: Alex"))) is None
    )
    assert "Alex" in store.read_profile()
    assert service.correct_fact("Name: Alex", "Name: Bob") is True
    assert "Bob" in store.read_profile() and "Alex" not in store.read_profile()
    assert service.forget_fact("Name: Bob") is True
    assert "Bob" not in store.read_profile()


def test_forget_during_profile_extraction_cannot_resurrect_old_fact(
    tmp_path: Path,
) -> None:
    class PausedLLM:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        async def generate(self, messages, tools=None):  # type: ignore[no-untyped-def]
            self.started.set()
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            return LLMResponse(content="[MEMORY.md] Name: Alex")

    async def scenario() -> str:
        store = PersonalMemoryStore(tmp_path / "agent")
        store.ensure_initialized()
        store.append_memory_md("\n- [legacy] Name: Alex\n")
        service = PersonalMemoryService(store)
        llm = PausedLLM()
        task = asyncio.create_task(service.ensure_profile(llm))
        while not llm.started.is_set():
            await asyncio.sleep(0.01)
        forget = asyncio.create_task(
            asyncio.to_thread(service.forget_fact, "Name: Alex")
        )
        await asyncio.sleep(0.05)
        llm.release.set()
        await task
        await forget
        return store.read_profile()

    profile = asyncio.run(scenario())
    assert "Alex" not in profile
