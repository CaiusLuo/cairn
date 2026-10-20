"""Request-budget values shared by the context view and the compaction policy.

These are plain data so both modules can depend on them without a cycle, and so
nothing that measures or compacts a request has to know about the other.
"""

from dataclasses import dataclass

#: Default request budget. Sized for the 128K window that current hosted models
#: offer: a smaller default fails real tasks before any history is trimmed, which
#: reads as a harness defect rather than a configuration choice. Callers that talk
#: to a narrower model must lower it.
DEFAULT_CONTEXT_MAX_TOKENS = 131072
DEFAULT_RESPONSE_MAX_TOKENS = 8192


@dataclass(frozen=True, slots=True)
class ContextBudget:
    max_tokens: int = DEFAULT_CONTEXT_MAX_TOKENS
    response_tokens: int = DEFAULT_RESPONSE_MAX_TOKENS

    def __post_init__(self) -> None:
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if type(self.response_tokens) is not int or self.response_tokens < 1:
            raise ValueError("response_tokens must be a positive integer")
        if self.response_tokens >= self.max_tokens:
            raise ValueError("response_tokens must be smaller than max_tokens")

    @property
    def input_tokens(self) -> int:
        """Largest input the request may carry and still be admitted."""
        return self.max_tokens - self.response_tokens


@dataclass(frozen=True, slots=True)
class TokenCount:
    tokens: int
    is_estimate: bool

    def __post_init__(self) -> None:
        if type(self.tokens) is not int or self.tokens < 0:
            raise ValueError("tokens must be a nonnegative integer")
        if type(self.is_estimate) is not bool:
            raise ValueError("is_estimate must be a boolean")
