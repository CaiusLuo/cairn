# M1-B Session and Turn lifecycle

`Session` is an opt-in, in-memory runtime API. It binds one exact Agent once,
admits one Turn at a time and observes settlement of the existing `run_turn`.
CLI, EvalSuite, TaskRunner, review and workflow callers remain stateless by
default. No SQLite, checkpoint, recovery, replay or background queue is created.

This executable offline example uses the ordinary client and event interfaces:

```python
import asyncio

from cairn.core.budget import RunBudget
from cairn.core.events import Event, SessionEvent
from cairn.core.models import LLMResponse
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn
from cairn.runtime.session import Session


class OfflineClient:
    async def generate(self, messages, tools=None):
        return LLMResponse(content="hello")


def observe(event: Event) -> None:
    if isinstance(event, SessionEvent):
        print(event.session_id, event.turn_id, event.sequence, event.type)


agent = Agent(OfflineClient(), ToolRegistry(), event_handler=observe)
session = Session(agent)
answer = asyncio.run(
    run_turn(agent, "hi", budget=RunBudget(max_steps=2), session=session)
)
assert answer == "hello" and session.last_turn.outcome == "completed"
session.close()
```

## Ownership and admission

Session and Turn IDs are independent host-generated UUID4 values, separate from
Trace and Task IDs. `Agent.session_owner` is read-only and `_bind_session` is
once-only. AgentState remains conversation-only. Attachment can preserve existing
history, but rejects stateless work in progress without changing its concurrency
policy. UUID equality grants no authority.

The first operation in `run_turn` checks the argument against the exact owner,
then synchronously reserves the guard before its first await, event, trace,
message append, approval or external effect. A supplied Session never auto-binds
an Agent. Missing, wrong, unrelated, closed and busy bindings reject with
`SessionAdmissionError` without execution or observations. Rebinding rejects.
Failed attachment after installation and close retain the owner forever;
bare-loop calls cannot silently downgrade to stateless execution.

| Session before | Operation | Session after |
| --- | --- | --- |
| Ready | Admit matching bound Turn | Executing |
| Executing | Complete, cancel, exhaust budget or fail; settle all work | Ready |
| Ready | Close | Closed |
| Executing | Close | Executing, `close_requested=True`; Closed after settlement |
| Closed | Close again | Closed |
| Closed / busy / wrong binding | Execute | Reject without transition |
| Attachment installed, then fails | Constructor raises | Owner remains Closed |

Close is synchronous and neither cancels nor detaches active work; the caller
can cancel its running task. `active_turn` remains installed through cleanup,
conversation settlement, trace finalization and terminal callback delivery.
`last_turn` is one frozen snapshot; later turns cannot modify a returned snapshot.
It retains ID, sequence bounds, outcome and failure flags, never raw input,
model text, arguments, outputs or credentials.

## Events and failures

The existing EventHandler receives `SessionEvent`, an Event subclass with typed
UUID `session_id`/`turn_id` and positive `sequence`. Existing payloads and streaming
step/attempt data remain intact. Dispatch is synchronous, with no retained buffer
or retry. Sequence increases across this Session object's lifetime, including
attempted deliveries whose callback raises; it is not a durable cursor.

`turn_started` is first, before trace creation. `turn_settled` follows owned
cleanup, rollback/pending-call settlement and the trace-finalization attempt.
The guard stays busy through callbacks, rejecting reentrant execution. Ordinary
`Agent.emit` cannot forge these owner-produced lifecycle events on a bound Agent.

| Terminal outcome | Meaning |
| --- | --- |
| completed | Agent returned its response; no Task/workspace correctness verdict |
| cancelled | Primary cancellation propagated after owned cleanup |
| budget_exhausted | Step or context admission budget rejected continuation |
| runtime_error | Execution or finalization failed |

Ordinary handler failures preserve the existing boundary: pre-tool failure rolls
back; executed tool facts survive. For bound turns, secondary trace-finalization
failure preserves an existing primary exception/cancellation, adds a static note
and sets `trace_finalization_failed`. Without a primary exception it propagates.
A sink failure captured by Tracer remains `trace_finish.persisted=False`.

The terminal callback cannot change the response, mask primary cancellation or
re-enter rollback. Exceptions set `last_turn.settlement_delivery_failed=True`,
without raw diagnostics or retry. Failed delivery may leave an observation gap;
the host can inspect `last_turn` in memory. Event payloads are not retained as
lifecycle metadata. Callback work must remain synchronous and bounded.

Future M1-C DurableSession reuses this owner/admission seam and adds its store
gate and receipt boundary. `turn_settled`, deltas, `agent_finish` and `trace_finish`
acknowledge no checkpoint. `turn_committed` remains reserved for a durable receipt;
see the [Durable Session design](../design/durable-session-v1.md).

## Verification and limits

```bash
uv run pytest tests/runtime/test_session.py
uv run pytest tests/runtime/test_loop_recovery.py tests/runtime/test_loop_streaming.py tests/runtime/test_loop_tracing.py
```

Follow [AGENTS.md](../../AGENTS.md) for related consumer, architecture and docs
checks. Tests use offline clients and asyncio Event barriers. This API assumes
one asyncio runtime host; cross-thread admission and process/crash recovery are
unsupported. Cleanup remains cooperative with existing timeout policies;
uncooperative tools/providers and blocking callbacks may delay settlement.
Permissions, provider bindings, ToolResult, trace schema and frozen eval contracts
remain unchanged.
