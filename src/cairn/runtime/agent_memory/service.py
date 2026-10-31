"""Personal AGENT.md/MEMORY.md lifecycle and bounded task journaling."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from cairn.core.agent_memory import (
    MemoryKind,
    PersonalMemoryContext,
)
from cairn.infra.agent_memory.store import AgentMemoryStore
from cairn.runtime.scheduling.runner import TaskOutcome, current_time

DEFAULT_MEMORY_AGENT_MD = """# Personal AGENT

This file belongs to this personal Agent, not the project repository.
Project AGENTS.md, system rules and permissions take precedence.

## Preferences
## Learned
"""


class PersonalMemoryService:
    def __init__(
        self,
        store: AgentMemoryStore,
        *,
        clock: Callable[[], datetime] = current_time,
        secret_values: Sequence[str] = (),
    ) -> None:
        self.store = store
        self.clock = clock
        self._secrets: list[str] = [v for v in secret_values if v]
        self.enabled = os.environ.get("CAIRN_AGENT_MEMORY", "on").lower() not in {
            "off",
            "0",
            "false",
        }
        if self.enabled:
            self.store.ensure_initialized()

    def bootstrap(self, now: datetime | None = None) -> PersonalMemoryContext:
        if not self.enabled:
            return PersonalMemoryContext()
        moment = now or self.clock()
        return PersonalMemoryContext(
            agent_md=self.redact(self.store.read_agent_md()),
            memory_md=self.redact(self.store.read_memory_md()),
            daily=tuple(
                (day, self.redact(body))
                for day, body in self.store.recent_daily(moment)
            ),
        )

    def render_context(self, now: datetime | None = None) -> str:
        """Empty until the personal store has content beyond its templates."""
        context = self.bootstrap(now)
        if (
            context.agent_md.strip() == DEFAULT_MEMORY_AGENT_MD.strip()
            and context.memory_md.strip() == "# Long-term Memory"
            and not any(body.strip() for _, body in context.daily)
        ):
            return ""
        return context.render()

    def add_secret_values(self, values: Sequence[str]) -> None:
        for value in values:
            if isinstance(value, str) and value and value not in self._secrets:
                self._secrets.append(value)
        self._secrets.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        cleaned = text
        for secret in self._secrets:
            if secret in cleaned:
                cleaned = cleaned.replace(secret, "[REDACTED]")
        return cleaned

    def system_prompt(self, base: str, now: datetime | None = None) -> str:
        context = self.render_context(now)
        return base + ("\n\n" + context if context else "")

    def record_task(
        self,
        *,
        prompt: str,
        result: TaskOutcome,
        task_id: str,
        trace_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if not self.enabled:
            return
        moment = now or self.clock()
        request = self.redact(" ".join(prompt.split())[:600])
        body = ""
        if result.final_response:
            body = self.redact(" ".join(result.final_response.split())[:1200])
        entry = (
            f"\n## {moment:%H:%M} task {task_id}\n"
            f"- kind: task\n"
            f"- source: agent_inferred\n"
            f"- status: {result.status.value}\n"
            f"- request: {request}\n"
            f"- result: {body or '(no final response)'}\n"
            f"- trace: {trace_id or result.trace_id or '(none)'}\n"
        )
        self.store.append_daily(moment.date(), entry)

    def record_turn(
        self,
        prompt: str,
        response: str | None,
        *,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if not self.enabled:
            return
        moment = now or self.clock()
        request = self.redact(" ".join(prompt.split())[:600])
        body = self.redact(" ".join((response or "").split())[:1200])
        entry = (
            f"\n## {moment:%H:%M} local turn\n"
            f"- kind: task\n"
            f"- source: agent_inferred\n"
            f"- status: completed\n"
            f"- task: {task_id or f'local-{moment:%Y%m%d%H%M%S}'}\n"
            f"- request: {request}\n"
            f"- result: {body or '(no final response)'}\n"
        )
        self.store.append_daily(moment.date(), entry)

    def record_note(
        self,
        text: str,
        *,
        kind: MemoryKind = MemoryKind.PREFERENCE,
        now: datetime | None = None,
    ) -> None:
        if not self.enabled:
            return
        moment = now or self.clock()
        clean = self.redact(" ".join(text.split()).strip())
        if not clean:
            raise ValueError("memory note cannot be empty")
        entry = (
            f"\n## {moment:%H:%M} note\n"
            f"- kind: {kind.value}\n"
            f"- source: user_explicit\n"
            f"- text: {clean[:2000]}\n"
        )
        self.store.append_daily(moment.date(), entry)

    @staticmethod
    def _normalize_fact(text: str) -> str:
        text = text.strip()
        if text.startswith("MEMORY.md:"):
            text = text[len("MEMORY.md:") :].strip()
        text = re.sub(r"^\[[^]]+\]\s*", "", text)
        text = re.sub(r"^\d{4}-\d{2}-\d{2}\s*:\s*", "", text)
        return " ".join(text.split()).casefold()

    def _candidates(
        self, *, days: int, include_memory: bool = False
    ) -> list[tuple[str, str, str]]:
        """Return ``(source_type, day, fact)`` candidates with provenance.

        Only explicit notes and explicit lessons promote by default. Ordinary
        task answers are retained in the daily journal but not promoted into
        long-term memory or AGENT.md proposals.
        """
        days = max(1, min(days, 31))
        seen: set[str] = set()
        candidates: list[tuple[str, str, str]] = []
        existing = self.store.read_memory_md()
        if include_memory:
            for line in existing.splitlines():
                stripped = line.strip()
                if not stripped.startswith("- "):
                    continue
                fact = stripped[2:].strip()
                key = self._normalize_fact(fact)
                if not key or key in seen:
                    continue
                candidates.append(("long_term", "MEMORY.md", fact[:500]))
                seen.add(key)
        for day in self.store.list_daily_days()[-days:]:
            body = self.store.read_daily(day)
            source_type = "agent_inferred"
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("## "):
                    source_type = "agent_inferred"
                    continue
                if stripped.startswith("- source:"):
                    source_type = stripped.split(":", 1)[1].strip()
                    continue
                if not stripped.startswith("- "):
                    continue
                text = stripped[2:].strip()
                if text.startswith("text:"):
                    value = self.redact(text[len("text:") :].strip())
                elif text.startswith("lesson:"):
                    value = self.redact(text[len("lesson:") :].strip())
                else:
                    # result:/status:/request:/trace: are journal facts, not
                    # automatically durable knowledge.
                    continue
                key = self._normalize_fact(value)
                if not key or key == "(no final response)" or key in seen:
                    continue
                candidates.append(
                    (source_type or "agent_inferred", day.isoformat(), value[:500])
                )
                seen.add(key)
                if len(candidates) >= 50:
                    break
            if len(candidates) >= 50:
                break
        return candidates

    def distill(self, *, days: int = 7, now: datetime | None = None) -> int:
        if not self.enabled:
            return 0
        moment = now or self.clock()
        existing = self.store.read_memory_md()
        existing_facts = {
            self._normalize_fact(line.strip()[2:])
            for line in existing.splitlines()
            if line.strip().startswith("- ")
        }
        candidates = [
            item
            for item in self._candidates(days=days, include_memory=False)
            if self._normalize_fact(item[2]) not in existing_facts
        ]
        if not candidates:
            return 0
        block = (
            f"\n## Distilled {moment:%Y-%m-%d}\n"
            + "\n".join(
                f"- [{origin}] {day}: {fact}" for origin, day, fact in candidates
            )
            + "\n"
        )
        self.store.append_memory_md(block)
        return len(candidates)

    def propose_from_memory(self, *, days: int = 7) -> str:
        """Generate an AGENT.md candidate from recent journal facts, with sources."""
        candidates = self._candidates(days=days, include_memory=True)
        if not candidates:
            raise ValueError("recent daily memory has no candidate facts")
        text = "\n".join(
            f"- [{origin}] {day}: {fact}" for origin, day, fact in candidates[:20]
        )
        return self.propose_agent_update(
            text,
            f"agent derived from {len(candidates)} daily-memory candidates",
        )

    def propose_agent_update(
        self, text: str, reason: str, now: datetime | None = None
    ) -> str:
        clean = self.redact(" ".join(text.split()).strip())
        if not clean:
            raise ValueError("AGENT.md proposal cannot be empty")
        existing = self.store.load_proposal()
        if existing is not None:
            raise ValueError("a pending AGENT.md proposal already exists")
        moment = now or self.clock()
        self.store.save_proposal(
            {
                "text": clean[:4000],
                "reason": self.redact(" ".join(reason.split())[:2000]),
                "created_at": moment.isoformat(),
            }
        )
        return clean[:4000]

    def approve_agent_update(self, now: datetime | None = None) -> str:
        if not self.enabled:
            raise ValueError("personal agent memory is disabled")
        proposal = self.store.load_proposal()
        if proposal is None:
            raise ValueError("no pending AGENT.md proposal")
        text = str(proposal.get("text", "")).strip()
        reason = str(proposal.get("reason", "")).strip()
        if not text:
            raise ValueError("pending proposal is empty")
        current = self.store.read_agent_md()
        # Keep user-written AGENT.md structure: append under Learned if present,
        # otherwise at the end. Never replace an existing section wholesale.
        if "## Learned" in current:
            before, after = current.split("## Learned", 1)
            updated = before + "## Learned" + after.rstrip() + "\n\n" + text + "\n"
        else:
            updated = current.rstrip() + "\n\n## Learned\n" + text + "\n"
        self.store.write_agent_md(updated, now=now or self.clock())
        self.store.append_audit(
            f"\n## {self.clock():%Y-%m-%d %H:%M:%S} approved AGENT.md update\n"
            f"- reason: {reason or '(none)'}\n"
            f"- text: {text[:2000]}\n"
        )
        self.store.clear_proposal()
        return text

    def rollback_agent_update(self) -> bool:
        changed = self.store.rollback_agent_md()
        if changed:
            self.store.append_audit(
                f"\n## {self.clock():%Y-%m-%d %H:%M:%S} rolled back AGENT.md\n"
            )
        return changed

    def pending_proposal(self) -> dict[str, Any] | None:
        return self.store.load_proposal()

    def status(self) -> dict[str, Any]:
        return {
            "agent_md": self.store.read_agent_md(),
            "memory_md": self.store.read_memory_md(),
            "daily_days": len(self.store.list_daily_days()),
            "pending": self.store.load_proposal(),
        }
