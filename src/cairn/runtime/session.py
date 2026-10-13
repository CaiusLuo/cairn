"""Opt-in, in-memory ownership and observation of the existing Agent turn."""

from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID, uuid4

from cairn.core.events import Event, SessionEvent
from cairn.runtime.agent import Agent
from cairn.runtime.agent import SessionAdmissionError as SessionAdmissionError


class SessionState(StrEnum):
    READY = "ready"
    EXECUTING = "executing"
    CLOSED = "closed"


class TurnOutcome(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RUNTIME_ERROR = "runtime_error"


@dataclass(frozen=True, slots=True)
class Turn:
    turn_id: UUID
    start_sequence: int
    outcome: TurnOutcome | None = None
    end_sequence: int | None = None
    trace_finalization_failed: bool = False
    settlement_delivery_failed: bool = False


class Session:
    """Bind once; the loop alone admits and settles turns. No event queue/store."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._session_id = uuid4()
        self._closed = False
        self._sequence = 0
        self._turn: Turn | None = None
        self._last_turn: Turn | None = None
        try:
            agent._bind_session(self, self._attribute)
        except BaseException:
            # If attachment failed after installing the owner, leave that exact
            # marker closed. No failure may silently restore stateless access.
            self._closed = True
            raise

    @property
    def session_id(self) -> UUID:
        return self._session_id

    @property
    def agent(self) -> Agent:
        return self._agent

    @property
    def state(self) -> SessionState:
        if self._turn is not None:
            return SessionState.EXECUTING
        return SessionState.CLOSED if self._closed else SessionState.READY

    @property
    def close_requested(self) -> bool:
        return self._closed

    @property
    def active_turn(self) -> Turn | None:
        return self._turn

    @property
    def last_turn(self) -> Turn | None:
        return self._last_turn

    def close(self) -> None:
        """Block admission now; keep active ownership through its settlement."""
        self._closed = True

    def _admit(self, agent: Agent) -> None:
        if self._agent is not agent or agent.session_owner is not self:
            raise SessionAdmissionError("Session does not own this Agent.")
        if self._closed:
            raise SessionAdmissionError("Session is closed.")
        if self._turn is not None:
            raise SessionAdmissionError("Session is executing a turn.")
        self._turn = Turn(uuid4(), start_sequence=self._sequence + 1)

    def _attribute(self, event: Event) -> Event:
        if event.type in ("turn_started", "turn_settled"):
            raise SessionAdmissionError("Lifecycle events belong to the turn owner.")
        return self._envelope(event)

    def _envelope(self, event: Event) -> Event:
        if self._turn is None:
            return event
        self._sequence += 1
        return SessionEvent(
            type=event.type,
            data=event.data,
            session_id=self._session_id,
            turn_id=self._turn.turn_id,
            sequence=self._sequence,
        )

    def _started(self) -> None:
        self._notify_lifecycle(Event(type="turn_started"))

    def _notify_lifecycle(self, event: Event) -> None:
        event = self._envelope(event)
        if self._agent.event_handler is not None:
            self._agent.event_handler(event)

    def _settle(self, outcome: TurnOutcome, trace_finalization_failed: bool) -> None:
        turn = self._turn
        assert turn is not None
        self._last_turn = replace(
            turn,
            outcome=outcome,
            end_sequence=self._sequence + 1,
            trace_finalization_failed=trace_finalization_failed,
        )
        try:
            self._notify_lifecycle(
                Event(
                    type="turn_settled",
                    data={
                        "outcome": outcome.value,
                        "trace_finalization_failed": trace_finalization_failed,
                    },
                )
            )
        except BaseException:
            # Post-settlement notification is not execution. Preserve the primary
            # result/cancellation and never re-enter rollback or redispatch.
            self._last_turn = replace(self._last_turn, settlement_delivery_failed=True)
        finally:
            self._last_turn = replace(self._last_turn, end_sequence=self._sequence)
            self._turn = None
