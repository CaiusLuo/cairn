from __future__ import annotations

import asyncio
from pathlib import Path

from cairn.agent.tools.base import ToolExecutionContext
from cairn.memory.models import MemorySource
from cairn.memory.service import PersonalMemoryService
from cairn.memory.store import PersonalMemoryStore
from cairn.memory.tools import MemoryRememberTool


def test_explicit_requires_trusted_user_text(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    service = PersonalMemoryService(store)
    tool = MemoryRememberTool(service)
    result = asyncio.run(
        tool.execute(
            {"text": "user prefers tabs", "explicit": True},
            context=ToolExecutionContext(
                trusted_user_text="remember user prefers tabs"
            ),
        )
    )
    assert result.exit_code == 0
    assert "user prefers tabs" in store.read_memory_md()


def test_model_cannot_forge_explicit_without_user_text(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    service = PersonalMemoryService(store)
    tool = MemoryRememberTool(service)
    result = asyncio.run(
        tool.execute(
            {"text": "inferred fact", "explicit": True},
            context=ToolExecutionContext(trusted_user_text="what is the weather"),
        )
    )
    assert result.exit_code == 0
    memory = store.read_memory_md()
    assert "inferred fact" in memory
    assert "[agent_inferred]" in memory
    assert "[user_explicit]" not in memory
    assert any(
        "inferred fact" in body
        for _, body in store.recent_daily_entries(service.clock())
    )


def test_non_boolean_explicit_and_extra_arguments_are_rejected(tmp_path: Path) -> None:
    tool = MemoryRememberTool(
        PersonalMemoryService(PersonalMemoryStore(tmp_path / "agent"))
    )
    for args in (
        {"text": "x", "explicit": "false"},
        {"text": "x", "extra": 1},
    ):
        try:
            asyncio.run(
                tool.execute(
                    args, context=ToolExecutionContext(trusted_user_text="记住")
                )
            )
        except Exception as exc:
            assert "explicit" in str(exc) or "accepts only" in str(exc)
        else:
            raise AssertionError("invalid arguments were accepted")


def test_exact_fact_without_memory_request_stays_inferred(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    service = PersonalMemoryService(store)
    tool = MemoryRememberTool(service)
    result = asyncio.run(
        tool.execute(
            {"text": "I like tea", "explicit": True},
            context=ToolExecutionContext(trusted_user_text="I like tea"),
        )
    )
    assert result.exit_code == 0
    memory = store.read_memory_md()
    assert "I like tea" in memory
    assert "[agent_inferred]" in memory
    assert "[user_explicit]" not in memory


def test_inferred_facts_share_one_learned_section(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    service = PersonalMemoryService(store)
    tool = MemoryRememberTool(service)
    for fact in ("likes tea", "likes coffee"):
        asyncio.run(tool.execute({"text": fact}))
    service.remember_fact("likes cocoa", source=MemorySource.USER_EXPLICIT)
    asyncio.run(tool.execute({"text": "likes water"}))
    memory = store.read_memory_md()
    assert memory.count("## Learned") == 1
    assert memory.count("[agent_inferred]") == 3
    assert memory.count("likes tea") == 1
    assert memory.index("likes water") < memory.index("## Remembered")


def test_negative_memory_request_does_not_authorize_explicit(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    tool = MemoryRememberTool(PersonalMemoryService(store))
    asyncio.run(
        tool.execute(
            {"text": "我喜欢喝茶", "explicit": True},
            context=ToolExecutionContext(trusted_user_text="不要记住这些,我喜欢喝茶"),
        )
    )
    assert "我喜欢喝茶" not in store.read_memory_md()


def test_common_positive_memory_request_authorizes_exact_fact(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    tool = MemoryRememberTool(PersonalMemoryService(store))
    asyncio.run(
        tool.execute(
            {"text": "我喜欢喝茶", "explicit": True},
            context=ToolExecutionContext(trusted_user_text="请记住我喜欢喝茶"),
        )
    )
    assert "我喜欢喝茶" in store.read_memory_md()


def test_negative_request_writes_nothing_at_all(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    before_memory = store.read_memory_md()
    before_daily = store.list_daily_days()
    tool = MemoryRememberTool(PersonalMemoryService(store))
    result = asyncio.run(
        tool.execute(
            {"text": "我喜欢喝茶", "explicit": True},
            context=ToolExecutionContext(trusted_user_text="不要记住这些,我喜欢喝茶"),
        )
    )
    assert result.exit_code == 0
    assert "declined" in result.stdout
    assert store.read_memory_md() == before_memory
    assert store.list_daily_days() == before_daily


def test_dont_forget_is_not_a_negative_request(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    tool = MemoryRememberTool(PersonalMemoryService(store))
    asyncio.run(
        tool.execute(
            {"text": "我喜欢喝茶", "explicit": True},
            context=ToolExecutionContext(trusted_user_text="别忘了我喜欢喝茶"),
        )
    )
    assert "我喜欢喝茶" in store.read_memory_md()
