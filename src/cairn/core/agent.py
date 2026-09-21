from cairn.llm.base import LLMClient
from cairn.core.state import AgentState
from cairn.tools.registry import ToolRegistry
from cairn.core.events import Event, EventHandler
from cairn.core.permissions import PermissionHandler

DEFAULT_SYSTEM_PROMPT = """
You are Cairn, a personal software engineering agent.

When working on code:
- Inspect before modifying.
- Make minimal changes.
- Verify changes when possible.
- Check the final diff before finishing.
- Do not claim success without evidence.
"""

class Agent:
    def __init__(
            self,
            llm: LLMClient,
            tools: ToolRegistry,
            system_prompt: str = DEFAULT_SYSTEM_PROMPT,
            event_handler: EventHandler | None = None,
            permission_handler: PermissionHandler | None = None
        ) -> AgentState:
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.event_handler = event_handler
        self.permission_handler = permission_handler
        self.state = AgentState()

    def emit(self, event: Event) -> None:
        if self.event_handler is not None:
            self.event_handler(event)