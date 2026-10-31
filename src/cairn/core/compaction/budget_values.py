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
    #: Extra input headroom for local estimates, as a percentage of the
    #: remaining window. Zero keeps existing programmatic budgets unchanged;
    #: the CLI configures the product default from models.toml.
    reserve_percent: int = 0

    def __post_init__(self) -> None:
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if type(self.response_tokens) is not int or self.response_tokens < 1:
            raise ValueError("response_tokens must be a positive integer")
        if self.response_tokens >= self.max_tokens:
            raise ValueError("response_tokens must be smaller than max_tokens")
        if type(self.reserve_percent) is not int or not 0 <= self.reserve_percent <= 50:
            raise ValueError("reserve_percent must be an integer between 0 and 50")

    @property
    def input_tokens(self) -> int:
        """Largest input the request may carry and still be admitted."""
        return self.max_tokens - self.response_tokens

    @property
    def estimated_input_tokens(self) -> int:
        """Input ceiling for an estimate, including the configured reserve."""
        return self.input_tokens * (100 - self.reserve_percent) // 100


@dataclass(frozen=True, slots=True)
class TokenCount:
    tokens: int
    is_estimate: bool

    def __post_init__(self) -> None:
        if type(self.tokens) is not int or self.tokens < 0:
            raise ValueError("tokens must be a nonnegative integer")
        if type(self.is_estimate) is not bool:
            raise ValueError("is_estimate must be a boolean")
