"""Carry a compaction across turns so consecutive turns do not repeat it.

Compaction records folded results and an optional prefix summary. The agent
reuses that view while its source messages remain unchanged, avoiding repeated
summaries and keeping rule-only reductions in subsequent requests.

The record is *not* a snapshot of the conversation. It stores only where the split
falls, what the replacement text says, and what it saved. The original messages
stay in ``AgentState`` untouched, which is what makes reuse safe: applying the
record again to a longer history is well defined, and dropping a stale record
loses nothing but the work.
"""

import hashlib
from dataclasses import dataclass

from cairn.core.compaction.rules import CompactionRecord
from cairn.core.models import Message


class StaleCompaction(ValueError):
    """A record no longer describes this conversation and must be discarded."""


@dataclass(frozen=True, slots=True)
class ReusableCompaction:
    """The last accepted substitution plus the history prefix it was built on.

    ``fingerprint`` identifies the prefix containing every affected message. Reuse is only valid
    while the conversation still begins with exactly those messages: history is
    append-only, so an unchanged prefix means the split point still means what it
    meant when the summary was written.

    ``conversation_length`` is how long the conversation was when this record was
    accepted. It answers a different question from reuse: whether anything has
    happened since, which is what decides if compacting again could achieve
    anything.
    """

    record: CompactionRecord
    fingerprint: tuple[str, ...]
    conversation_length: int

    @classmethod
    @classmethod
    def capture(
        cls, history: list[Message], record: CompactionRecord
    ) -> "ReusableCompaction":
        """Remember ``record`` against the prefix containing its source messages."""
        prefix_end = max(
            record.retained_from, max(record.folded_results, default=-1) + 1
        )
        replaced = history[:prefix_end]
        if len(replaced) != prefix_end:
            raise StaleCompaction("record reaches past the conversation")
        return cls(
            record=record,
            fingerprint=_fingerprint(replaced),
            conversation_length=len(history),
        )

    def reusable_with(self, history: list[Message]) -> bool:
        """Whether ``history`` still begins with the prefix this record replaced."""
        if len(history) < self.conversation_length:
            return False
        return _fingerprint(history[: len(self.fingerprint)]) == self.fingerprint

    def is_current_for(self, history: list[Message]) -> bool:
        """Whether nothing has been appended since this compaction was accepted.

        A repeat with no new messages cannot produce a different summary, so the
        caller can report the existing one instead of paying for the same work.
        """
        return self.reusable_with(history) and len(history) == self.conversation_length

    def record_for(self, history: list[Message]) -> CompactionRecord:
        """Return the record to apply, or raise when it has gone stale.

        Raises rather than silently reusing nothing: a caller that had a record
        and lost it needs to know it is about to pay for the summary again.
        """
        if not self.reusable_with(history):
            raise StaleCompaction("history no longer matches this compaction")
        return self.record


def _fingerprint(messages: list[Message]) -> tuple[str, ...]:
    """A compact, order-sensitive identity for a message prefix.

    Include complete tool-call identities and arguments as well as result bodies,
    without retaining another copy of a potentially large conversation.
    """
    return tuple(
        hashlib.sha256(message.model_dump_json().encode("utf-8")).hexdigest()
        for message in messages
    )
