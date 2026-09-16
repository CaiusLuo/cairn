from pydantic import BaseModel, Field

from cairn.models import Message

class AgentState(BaseModel):
    messages: list[Message] = Field(default_factory=list)

    def add_user_message(self, content: str) -> None:
        self.messages.append(
            Message(
                role="user", 
                content=content
            )
        )

    def add_assistant_message(self, content: str) -> None:
        self.messages.append(
            Message(
                role="assistant", 
                content=content
            )
        )