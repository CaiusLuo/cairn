"""A turn may contain more than one user message.

Steering adds an instruction to a turn that is already running, so a turn is no
longer exactly one user message. The old invariant said a turn may contain only
one, because the context builder needed a single index to mark where the current
turn begins.

That index still exists and still points at the *first* user message of the turn;
what changes is that later user messages inside the same turn are allowed. The
rules the builder depends on are narrower than "exactly one", so they are stated
again precisely here:

* the index must name a user message,
* the conversation must start with a user message,
* and every user message *before* the index closes an older turn, so trimming can
  still drop whole old turns.

Trimming is what these tests protect: allowing a second user message must not make
the builder drop part of the live turn.
"""

from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.models import Message


def _builder(
    max_tokens: int = 100_000, *, response_tokens: int = 1024
) -> ContextBuilder:
    return ContextBuilder(
        budget=ContextBudget(max_tokens=max_tokens, response_tokens=response_tokens)
    )


def _user(text: str) -> Message:
    return Message(role="user", content=text)


def _assistant(text: str) -> Message:
    return Message(role="assistant", content=text)


def test_a_turn_with_one_user_message_still_builds() -> None:
    history = [_user("hello"), _assistant("hi")]
    request = _builder().build(
        system_messages=[], history=history, current_turn_start=0, tools=[]
    )
    assert [message.content for message in request.messages] == ["hello", "hi"]


def test_a_second_user_message_inside_the_turn_is_allowed() -> None:
    """The case steering creates: an instruction joins a turn already running."""
    history = [
        _user("start"),
        _assistant("working"),
        _user("actually use the other module"),
    ]
    request = _builder().build(
        system_messages=[], history=history, current_turn_start=0, tools=[]
    )
    contents = [message.content for message in request.messages]
    assert contents == ["start", "working", "actually use the other module"]


def test_an_old_turn_is_still_dropped_whole_when_trimming() -> None:
    """Old turns remain droppable, even though the live turn now holds a steer.

    Trimming omits *whole* old turns and replaces them with one notice, so the case
    that must actually be exercised is one where dropping pays for itself: enough
    old content that the notice is cheaper than what it replaces.
    """
    history: list[Message] = []
    for index in range(6):
        history.append(_user(f"old task {index} with enough words to matter"))
        history.append(_assistant(f"old answer {index} also reasonably long"))
    history.append(_user("new task"))
    history.append(_assistant("working"))
    history.append(_user("steered instruction"))
    current = len(history) - 3

    request = _builder(max_tokens=520, response_tokens=50).build(
        system_messages=[], history=history, current_turn_start=current, tools=[]
    )
    contents = [message.content for message in request.messages if message.content]

    # Whole old turns are gone, and the live turn keeps everything it accumulated.
    assert not any("old task 0" in text for text in contents)
    assert "new task" in contents
    assert "steered instruction" in contents
    assert request.omitted_turns > 0


def test_the_live_turn_is_never_partially_dropped() -> None:
    """Trimming removes whole old turns; a steer belongs to the live one."""
    history = [
        _user("old"),
        _assistant("answer"),
        _user("current"),
        _assistant("step"),
        _user("steer"),
        _assistant("final"),
    ]
    request = _builder(max_tokens=600, response_tokens=50).build(
        system_messages=[], history=history, current_turn_start=2, tools=[]
    )
    contents = [message.content for message in request.messages if message.content]
    assert "current" in contents
    assert "steer" in contents
    assert "final" in contents
