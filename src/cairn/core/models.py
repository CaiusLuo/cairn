from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class Message(BaseModel):
    role: Literal[
        "system",
        "user",
        "assistant",
        "tool",
    ]

    content: str | None = None
    tool_call_id: str | None = None

    tool_calls: list[ToolCall] = Field(default_factory=list)


class LLMUsage(BaseModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    # Tokens the provider served from its prompt cache. Providers report this
    # only when caching applies, so None means "not reported", not "zero".
    cached_tokens: int | None = Field(default=None, ge=0)
    # Tokens spent on reasoning, when the provider separates them from the answer.
    reasoning_tokens: int | None = Field(default=None, ge=0)


class LLMResponse(BaseModel):
    content: str | None = None

    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: LLMUsage | None = None


class ToolResult(BaseModel):
    stdout: str = ""
    stderr: str = ""
    exit_code: int

    stdout_truncated: bool = False
    stderr_truncated: bool = False

    #: A model-facing outcome class when the result needs one beyond the exit
    #: code. ``permission_required`` tells the model an approval would unblock
    #: the call, so it can ask the operator instead of assuming it was malformed.
    error_type: str | None = None
    #: Targets the caller was not allowed to touch. Present with
    #: ``error_type="permission_required"`` so the model can name what it needs.
    blocked_paths: tuple[str, ...] = ()


class ToolFailure(BaseModel):
    error: str
    type: str
    #: Targets the caller may not touch yet. Present for
    #: ``type="permission_required"`` so the model can name what it needs.
    blocked_paths: tuple[str, ...] = ()

    def to_content(self) -> str:
        return self.model_dump_json(ensure_ascii=False)
