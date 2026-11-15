"""Results of an attempt to send a message outside an inbound reply."""

from dataclasses import dataclass
from enum import StrEnum


class ProactiveSendOutcome(StrEnum):
    SENT = "sent"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ProactiveSendResult:
    outcome: ProactiveSendOutcome
    error: str | None = None
    retryable: bool = False
    attempts: int | None = None

    @property
    def sent(self) -> bool:
        return self.outcome is ProactiveSendOutcome.SENT
