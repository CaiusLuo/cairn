"""Carry a compaction across turns so consecutive turns do not repeat it.

Compaction is a substitution: everything before ``retained_from`` is replaced by
one summary. Recomputing that substitution at the start of every turn would pay
for the same summary again and again while the conversation merely grows at its
end, so the agent keeps the last accepted record and reuses it while it still
describes the same history.

The record is *not* a snapshot of the conversation. It stores only where the split
falls, what the replacement text says, and what it saved. The original messages
stay in ``AgentState`` untouched, which is what makes reuse safe: applying the
record again to a longer history is well defined, and dropping a stale record
loses nothing but the work.
"""

from dataclasses import dataclass

from cairn.core.compaction.rules import CompactionRecord
from cairn.core.models import Message


class StaleCompaction(ValueError):
    """A record no longer describes this conversation and must be discarded."""


@dataclass(frozen=True, slots=True)
class ReusableCompaction:
    """The last accepted substitution plus the history prefix it was built on.

    ``fingerprint`` holds the messages the record replaced. Reuse is only valid
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
        """Remember ``record`` against the prefix it replaces."""
        replaced = history[: record.retained_from]
        if len(replaced) != record.retained_from:
            raise StaleCompaction("record reaches past the conversation")
        return cls(
            record=record,
            fingerprint=_fingerprint(replaced),
            conversation_length=len(history),
        )

    def reusable_with(self, history: list[Message]) -> bool:
        """Whether ``history`` still begins with the prefix this record replaced."""
        if len(history) < self.record.retained_from:
            return False
        return _fingerprint(history[: self.record.retained_from]) == self.fingerprint

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

    Roles and bodies identify a message well enough to detect a rewritten or
    truncated history, without retaining a second copy of the conversation.
    """
    return tuple(
        f"{message.role}\x00{message.tool_call_id or ''}\x00{message.content or ''}"
        for message in messages
    )
