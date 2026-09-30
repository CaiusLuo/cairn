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


class ToolFailure(BaseModel):
    error: str
    type: str

    def to_content(self) -> str:
        return self.model_dump_json(ensure_ascii=False)
