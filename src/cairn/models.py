from typing import Literal, Any

from pydantic import BaseModel, Field

class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]

class LLMResponse(BaseModel):
    content: str
    tool_calls: list[ToolCall] = Field(
        default_factory=list
    )

class ToolResult(BaseModel):
    stdout: str = ""
    stderr: str = ""
    exit_code: int