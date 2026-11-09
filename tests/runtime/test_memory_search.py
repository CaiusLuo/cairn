from __future__ import annotations

from pathlib import Path

from cairn.memory.service import PersonalMemoryService
from cairn.memory.store import PersonalMemoryStore


def _service(tmp_path: Path) -> PersonalMemoryService:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md(
        "\n- 姓名: 张三\n- favorite editor: Vim\n- project: cairn memory\n"
    )
    return PersonalMemoryService(store)


def test_local_search_chinese_short_word_english_name_and_multi_keyword(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    assert any("张三" in fact for _, _, fact in service.search_facts("张"))
    assert any("Vim" in fact for _, _, fact in service.search_facts("Vim"))
    matches = service.search_facts("cairn memory")
    assert matches and "cairn memory" in matches[0][2]


def test_no_direct_match_returns_labeled_long_term_candidates(tmp_path: Path) -> None:
    service = _service(tmp_path)
    matches = service.search_facts("完全不存在的词")
    assert matches
    assert all("long-term candidate" in source for source, _, _ in matches)
    assert len(matches) <= 5


def test_forget_and_correct_require_exact_fact_match(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.forget_fact("张") is False
    assert service.forget_fact("张三") is True
    assert "张三" not in service.store.read_memory_md()
    assert service.correct_fact("Vim", "Neovim") is True
    assert "Neovim" in service.store.read_memory_md()
    assert service.correct_fact("V", "Emacs") is False


def test_identity_query_falls_back_to_labeled_candidates_not_single_char_noise(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    matches = service.search_facts("我是谁")
    text = "\n".join(fact for _, _, fact in matches)
    assert "张三" in text
    assert all("long-term candidate" in source for source, _, _ in matches)


def test_search_redacts_known_secrets(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    store.ensure_initialized()
    store.append_memory_md("\n- secret: fake-provider-key\n")
    service = PersonalMemoryService(store, secret_values=("fake-provider-key",))
    matches = service.search_facts("secret")
    assert matches and all("fake-provider-key" not in fact for _, _, fact in matches)
    assert any("[REDACTED]" in fact for _, _, fact in matches)


def test_correct_fact_replaces_daily_candidate_before_distill(tmp_path: Path) -> None:
    store = PersonalMemoryStore(tmp_path / "agent")
    service = PersonalMemoryService(store)
    service.remember_fact("prefers tea")
    assert service.correct_fact("prefers tea", "prefers coffee") is True
    daily = "\n".join(body for _, body in store.recent_daily_entries(service.clock()))
    assert "prefers coffee" in daily
    assert "prefers tea" not in daily
    assert service.distill(days=1) == 1
    memory = store.read_memory_md()
    assert "prefers coffee" in memory
    assert "prefers tea" not in memory
