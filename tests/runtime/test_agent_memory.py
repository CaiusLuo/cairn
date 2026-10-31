"""Personal agent memory: private Markdown, journaling, distillation, evolution."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cairn.core.agent_memory import MemoryKind
from cairn.infra.agent_memory.store import AgentMemoryStore
from cairn.runtime.agent_memory.service import PersonalMemoryService
from cairn.terminal.headless import HeadlessResult, HeadlessStatus

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _service(tmp_path: Path) -> tuple[PersonalMemoryService, AgentMemoryStore]:
    store = AgentMemoryStore(tmp_path / "agent")
    return PersonalMemoryService(store, clock=lambda: NOW), store


def test_store_initializes_private_files_and_bootstrap_loads_today(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    assert store.agent_md_path.exists()
    assert store.memory_md_path.exists()
    service.record_note("prefers short replies", kind=MemoryKind.PREFERENCE, now=NOW)
    context = service.bootstrap(NOW)
    assert "prefers short replies" in context.render()
    assert context.daily and "prefers short replies" in context.daily[0][1]
    assert oct(store.agent_md_path.stat().st_mode & 0o777) == "0o600"
    assert oct(store.root.stat().st_mode & 0o777) == "0o700"


def test_task_journal_and_distill_write_memory_md(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    result = HeadlessResult(
        task_id="task-1",
        status=HeadlessStatus.COMPLETED,
        final_response="fixed note",
        workspace=str(tmp_path),
    )
    service.record_task(
        prompt="fix note", result=result, task_id="task-1", trace_id="trace-1", now=NOW
    )
    service.record_note("remember the fix", now=NOW)
    daily = store.read_daily(NOW.date())
    assert "task-1" in daily and "fixed note" in daily and "trace-1" in daily
    count = service.distill(days=1, now=NOW)
    assert count > 0
    assert "Distilled" in store.read_memory_md()


def test_agent_proposal_requires_approval_and_supports_rollback(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    original = store.read_agent_md()
    service.propose_agent_update("Prefer terse answers.", "test")
    assert service.pending_proposal() is not None
    assert store.read_agent_md() == original

    service.approve_agent_update(now=NOW)
    assert "Prefer terse answers." in store.read_agent_md()
    assert service.pending_proposal() is None
    assert "approved AGENT.md update" in store.audit_path.read_text()

    assert service.rollback_agent_update() is True
    assert store.read_agent_md() == original
    assert "rolled back AGENT.md" in store.audit_path.read_text()


def test_local_turn_is_journaled_for_cross_task_continuity(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    service.record_turn("what changed?", "updated note.txt", now=NOW)
    daily = store.read_daily(NOW.date())
    assert "local turn" in daily and "updated note.txt" in daily


def test_agent_can_generate_agent_proposal_from_daily_memory(tmp_path: Path) -> None:
    service, _store = _service(tmp_path)
    service.record_note("Use concise answers", now=NOW)
    proposal = service.propose_from_memory(days=1)
    assert "Use concise answers" in proposal
    assert NOW.date().isoformat() in proposal
    assert service.pending_proposal() is not None


def test_repeated_distill_writes_nothing_new_and_does_not_nest(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    service.record_note("unique preference", now=NOW)
    assert service.distill(days=1, now=NOW) == 1
    assert service.distill(days=1, now=NOW) == 0
    assert service.distill(days=1, now=NOW) == 0
    memory = store.read_memory_md()
    assert memory.count("unique preference") == 1
    assert "MEMORY.md:" not in memory


def test_personal_memory_survives_a_new_service_instance(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    service.record_note("survives restart", now=NOW)
    restarted = PersonalMemoryService(store, clock=lambda: NOW)
    rendered = restarted.render_context(NOW)
    assert "survives restart" in rendered


def test_render_keeps_valid_json_and_recent_daily_under_chinese_growth() -> None:
    import json

    from cairn.core.agent_memory import PersonalMemoryContext

    context = PersonalMemoryContext(
        agent_md="中" * 4400,
        daily=(("2026-11-01", "今天的日报: 完成登录"),),
        max_chars=12000,
    )
    rendered = context.render()
    assert len(rendered) <= 12000
    payload = json.loads(rendered.split("\n", 1)[1])
    daily = payload["personal_agent_memory"]["daily"]
    assert daily and "今天的日报" in daily[0]["text"]


def test_known_secrets_are_redacted_before_write_and_render(tmp_path: Path) -> None:
    store = AgentMemoryStore(tmp_path / "agent")
    service = PersonalMemoryService(
        store, clock=lambda: NOW, secret_values=("fake-provider-secret",)
    )
    service.record_note("token is fake-provider-secret", now=NOW)
    result = HeadlessResult(
        task_id="task-secret",
        status=HeadlessStatus.COMPLETED,
        final_response="answer includes fake-provider-secret",
        workspace=str(tmp_path),
    )
    service.record_task(
        prompt="use fake-provider-secret", result=result, task_id="task-secret", now=NOW
    )
    daily = store.read_daily(NOW.date())
    rendered = service.render_context(NOW)
    assert "fake-provider-secret" not in daily
    assert "fake-provider-secret" not in rendered
    assert "[REDACTED]" in daily


def test_distill_skips_plain_task_answer_and_keeps_source_type(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    service.record_note("用户明确偏好简洁回答", now=NOW)
    result = HeadlessResult(
        task_id="task-hello",
        status=HeadlessStatus.COMPLETED,
        final_response="Hello there!",
        workspace=str(tmp_path),
    )
    service.record_task(
        prompt="say hello", result=result, task_id="task-hello", now=NOW
    )
    assert service.distill(days=1, now=NOW) == 1
    memory = store.read_memory_md()
    assert "用户明确偏好简洁回答" in memory
    assert "Hello there!" not in memory
    assert "[user_explicit]" in memory


def test_large_daily_keeps_new_note_visible_and_distillable(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    path = store.daily_path(NOW.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("A" * 25000 + "\n", encoding="utf-8")
    service.record_note("new preference at end", now=NOW)
    assert "new preference at end" in service.render_context(NOW)
    assert service.distill(days=1, now=NOW) == 1


def test_large_agent_md_is_preserved_by_approval_and_rollback(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    original = "HEAD\n" + ("A" * 25000) + "\nTAIL-SENTINEL\n"
    store.agent_md_path.write_text(original, encoding="utf-8")
    service.propose_agent_update("new learned line", "test")
    service.approve_agent_update(now=NOW)
    approved = store.read_agent_md()
    assert "TAIL-SENTINEL" in approved
    assert "new learned line" in approved
    assert service.rollback_agent_update() is True
    assert store.read_agent_md() == original


def test_crlf_agent_md_rollback_is_byte_exact(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    original = b"HEAD\r\n" + (b"A" * 25000) + b"\r\nTAIL\r\n"
    store.agent_md_path.write_bytes(original)
    service.propose_agent_update("new line", "test")
    service.approve_agent_update(now=NOW)
    assert service.rollback_agent_update() is True
    assert store.agent_md_path.read_bytes() == original
