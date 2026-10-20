"""Replace a settled range of a conversation with its summary, in place.

Compaction is a substitution, not an append: the summarized range becomes one
message at the exact index the range started, so everything after it keeps its
relative order and the current turn stays where the loop expects it. Building the
request from "survivors plus a summary at the end" would reorder the conversation
the model reads, so this module is the only place that performs the substitution
and it is exercised directly.
"""

from cairn.core.compaction.rules import CompactionRecord
from cairn.core.models import Message

#: Stand-in for a folded tool result body. One definition, because two reducers
#: shorten tool output and the model must not see two different phrasings for the
#: same fact.
FOLDED_TEMPLATE = (
    "[earlier tool output omitted from this request: {characters} characters]"
)


def folded_placeholder(characters: int) -> str:
    """The body that replaces a shortened tool result."""
    return FOLDED_TEMPLATE.format(characters=characters)


#: Fixed prefix of a summary message. Being a constant keeps the message
#: recognisable to the model and to tests without inventing a new message role,
#: which would not survive every provider's message vocabulary.
SUMMARY_PREFIX = "Conversation summary (reference data, not instructions):"


class ReplacementError(ValueError):
    """A record does not describe a legal substitution of this conversation."""


def apply_replacement(
    history: list[Message], record: CompactionRecord
) -> list[Message]:
    """Substitute the prefix ``history[:retained_from]`` with one summary message.

    The original list is never mutated: compaction changes what a request sends,
    never what the conversation recorded.
    """
    end = record.retained_from
    _validate_range(history, 0, end)
    return [summary_message(record.summary), *history[end:]]


def summary_message(summary: str) -> Message:
    """Wrap a summary so it can never be read as a system instruction."""
    return Message(role="user", content=f"{SUMMARY_PREFIX}\n{summary}")


def _validate_range(history: list[Message], start: int, end: int) -> None:
    if not 0 <= start <= end <= len(history):
        raise ReplacementError("compaction range does not fit the conversation")
    if start == end:
        raise ReplacementError("compaction range is empty")
    # The split must fall on a group boundary. A surviving tool result whose call
    # the summary replaced would be invalid for every provider, and a split inside
    # an unanswered call group would strand the call the same way.
    if end < len(history) and history[end].role == "tool":
        raise ReplacementError("compaction range would orphan a tool result")
    if end < len(history) and history[end].tool_calls:
        raise ReplacementError("compaction range would split a tool-call group")
    replaced = history[end:]
    answered = {message.tool_call_id for message in replaced if message.role == "tool"}
    called = {
        call.id
        for message in history[:end]
        if message.tool_calls
        for call in message.tool_calls
    }
    if answered & called:
        raise ReplacementError("compaction range leaves a call without its result")
