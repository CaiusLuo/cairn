from typing import Literal

from pydantic import BaseModel

class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class LLMResponse(BaseModel):
    content: str

class ToolResult(BaseModel):
    stdout: str = ""
    stderr: str = ""
    exit_code: int