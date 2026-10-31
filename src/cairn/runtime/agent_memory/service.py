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
        agent_md = self.redact(self.store.read_agent_md())
        return PersonalMemoryContext(
            profile=self._profile(agent_md, self.redact(self.store.read_memory_md())),
            agent_md=agent_md,
            memory_md=self.redact(self.store.read_memory_md()),
            daily=tuple(
                (day, self.redact(body))
                for day, body in self.store.recent_daily_entries(moment)
            ),
        )

    @staticmethod
    def _profile(agent_md: str, memory_md: str = "") -> str:
        """Stable profile facts, falling back to existing long-term records."""
        lines = agent_md.splitlines()
        values: list[str] = []
        in_preferences = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                in_preferences = stripped.casefold() == "## preferences"
                continue
            if in_preferences and stripped and not stripped.startswith("#"):
                values.append(stripped.removeprefix("- ").strip())
            if len(values) >= 12:
                break
        if not values and agent_md.strip() != DEFAULT_MEMORY_AGENT_MD.strip():
            values = [
                line.strip()
                for line in lines
                if line.strip() and not line.lstrip().startswith("#")
            ][:5]
        if not values:
            # Existing installations may keep the identity only in MEMORY.md.
            values = [
                line.strip()[2:].strip()
                for line in memory_md.splitlines()
                if line.strip().startswith("- ")
            ][:5]
        return "\n".join(values)[:600]

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
        if text.startswith("text:"):
            text = text[len("text:") :].strip()
        elif text.startswith("lesson:"):
            text = text[len("lesson:") :].strip()
        return " ".join(text.split()).casefold()

    @classmethod
    def _fact_keys(cls, text: str) -> frozenset[str]:
        """Exact keys for a fact: full normalized text and a labeled value.

        Deletion/correction compares these keys for equality only; it never
        performs a substring or fuzzy search.
        """
        full = cls._normalize_fact(text)
        keys = {full} if full else set()
        if ": " in full:
            suffix = full.split(": ", 1)[1].strip()
            if suffix:
                keys.add(suffix)
        return frozenset(keys)

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

    @staticmethod
    def _search_tokens(query: str) -> tuple[str, ...]:
        """Local tokenization for CJK short words and English names.

        No vector store or second model is involved. A CJK run contributes the
        whole run plus its characters and adjacent bigrams, so a short Chinese
        name still matches without hardcoding names.
        """
        normalized = " ".join(query.split()).casefold()
        tokens: list[str] = [normalized] if normalized else []
        for word in re.findall(r"[a-z0-9_]+", normalized):
            if word not in tokens:
                tokens.append(word)
        for run in re.findall(r"[\u3400-\u9fff]+", normalized):
            if run not in tokens:
                tokens.append(run)
            for char in run:
                if char not in tokens:
                    tokens.append(char)
            for index in range(len(run) - 1):
                pair = run[index : index + 2]
                if pair not in tokens:
                    tokens.append(pair)
        return tuple(tokens)

    def search_facts(self, query: str, *, limit: int = 8) -> list[tuple[str, str, str]]:
        """Return ``(source, date, fact)`` matches from local memory only.

        Direct matches are ranked first. When nothing matches directly, a small
        set of long-term facts is returned explicitly marked as unconfirmed
        candidates for the current conversation model to judge.
        """
        if not self.enabled:
            return []
        normalized = " ".join(query.split()).casefold()
        if not normalized:
            return []
        tokens = self._search_tokens(normalized)
        facts: list[tuple[str, str, str]] = []
        for line in self.store.read_memory_md().splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                facts.append(("MEMORY.md", "", self.redact(stripped[2:].strip())))
        for day in self.store.list_daily_days()[-31:]:
            for line in self.store.read_daily(day).splitlines():
                stripped = line.strip()
                if stripped.startswith("- text:") or stripped.startswith("- lesson:"):
                    facts.append(
                        (
                            day.isoformat(),
                            "",
                            self.redact(stripped.split(":", 1)[1].strip()),
                        )
                    )
        scored: list[tuple[int, str, str, str]] = []
        exact_match = False
        for source, date, fact in facts:
            folded = fact.casefold()
            exact = normalized in folded
            exact_match = exact_match or exact
            # A single generic CJK character is not enough to claim a direct
            # hit; it may still seed the clearly-labelled candidate fallback.
            token_hits = [
                token
                for token in tokens
                if token and token in folded and (len(token) >= 2 or exact)
            ]
            if exact:
                score = 100
            elif token_hits:
                score = len(token_hits)
            else:
                score = 0
            if score:
                scored.append((score, source, date, fact))
        scored.sort(key=lambda item: item[0], reverse=True)
        direct = [(source, date, fact) for _, source, date, fact in scored[:limit]]
        if exact_match:
            return direct
        # A weak token hit can be accidental (e.g. "我是" inside a sentence), so
        # attach a small, explicitly unconfirmed set of long-term candidates for
        # the current model to judge. This is not a second search model.
        seen = {(source, fact) for source, _, fact in direct}
        candidates = [
            ("MEMORY.md (long-term candidate; not confirmed)", "", fact)
            for source, _, fact in facts
            if source == "MEMORY.md" and ("MEMORY.md", fact) not in seen
        ][:5]
        return (direct + candidates)[: limit + 5]

    def remember_fact(self, text: str, *, now: datetime | None = None) -> bool:
        """Agent-authored durable fact; duplicate facts are ignored."""
        if not self.enabled:
            raise ValueError("personal agent memory is disabled")
        clean = self.redact(" ".join(text.split()).strip())
        if not clean:
            raise ValueError("memory fact cannot be empty")
        existing = self.store.read_memory_md()
        if any(
            self._normalize_fact(line.strip()[2:]) == self._normalize_fact(clean)
            for line in existing.splitlines()
            if line.strip().startswith("- ")
        ):
            return False
        moment = now or self.clock()
        self.store.append_memory_md(
            f"\n## Remembered {moment:%Y-%m-%d}\n"
            f"- [agent] {moment.date().isoformat()}: {clean[:2000]}\n"
        )
        self.store.append_audit(
            f"\n## {moment:%Y-%m-%d %H:%M:%S} remembered fact\n- text: {clean[:2000]}\n"
        )
        return True

    def forget_fact(self, query: str, *, now: datetime | None = None) -> bool:
        """Remove matching facts from long-term memory and recent daily candidates."""
        if not self.enabled:
            raise ValueError("personal agent memory is disabled")
        needle = self._normalize_fact(query)
        if not needle:
            raise ValueError("memory query cannot be empty")
        moment = now or self.clock()
        changed = False
        memory = self.store.read_memory_md()
        kept = []
        for line in memory.splitlines():
            if line.strip().startswith("- ") and needle in self._fact_keys(
                line.strip()[2:]
            ):
                changed = True
                continue
            kept.append(line)
        if changed:
            self.store.write_memory_md("\n".join(kept).rstrip() + "\n")
        for day in self.store.list_daily_days()[-31:]:
            body = self.store.read_daily(day)
            lines = []
            day_changed = False
            for line in body.splitlines():
                if needle in self._fact_keys(line):
                    day_changed = True
                    continue
                lines.append(line)
            if day_changed:
                changed = True
                self.store.write_daily(day, "\n".join(lines).rstrip() + "\n")
        if changed:
            self.store.append_audit(
                f"\n## {moment:%Y-%m-%d %H:%M:%S} forgot fact\n"
                f"- query: {self.redact(query)[:500]}\n"
            )
        return changed

    def correct_fact(self, old: str, new: str, *, now: datetime | None = None) -> bool:
        """Replace a matching fact and keep the correction auditable."""
        if not self.enabled:
            raise ValueError("personal agent memory is disabled")
        needle = self._normalize_fact(old)
        replacement = self.redact(" ".join(new.split()).strip())
        if not needle or not replacement:
            raise ValueError("old and new memory facts must be non-empty")
        moment = now or self.clock()
        changed = False
        memory = self.store.read_memory_md()
        lines = []
        for line in memory.splitlines():
            if line.strip().startswith("- ") and needle in self._fact_keys(
                line.strip()[2:]
            ):
                lines.append(
                    f"- [agent] {moment.date().isoformat()}: {replacement[:2000]}"
                )
                changed = True
            else:
                lines.append(line)
        if changed:
            self.store.write_memory_md("\n".join(lines).rstrip() + "\n")
        if changed:
            self.store.append_audit(
                f"\n## {moment:%Y-%m-%d %H:%M:%S} corrected fact\n"
                f"- old: {self.redact(old)[:500]}\n- new: {replacement[:2000]}\n"
            )
        return changed

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
