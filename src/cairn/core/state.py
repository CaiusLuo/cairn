from pydantic import BaseModel, Field

from cairn.core.models import Message, ToolCall

class AgentState(BaseModel):
    messages: list[Message] = Field(default_factory=list)

    def add_user_message(self, content: str) -> None:
        self.messages.append(
            Message(
                role="user", 
                content=content
            )
        )

    def add_assistant_message(
            self, 
            content: str,
            tool_calls: list[ToolCall] | None = None,
        ) -> None:
        self.messages.append(
            Message(
                role="assistant", 
                content=content,
                tool_calls=tool_calls or []
            )
        )

    def add_tool_message(
            self,
            tool_call_id: str,
            content: str,
        ) -> None:
        self.messages.append(
            Message(
                role="tool",
                tool_call_id=tool_call_id,
                content=content,
            )
        )