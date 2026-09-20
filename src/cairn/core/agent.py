from cairn.llm.base import LLMClient
from cairn.core.state import AgentState
from cairn.tools.registry import ToolRegistry
from cairn.core.events import Event, EventHandler

DEFAULT_SYSTEM_PROMPT = """
You are Cairn, a personal agent.
A Cairn marks the path for whoever comes next. So does a harness.
"""

class Agent:
    def __init__(
            self,
            llm: LLMClient,
            tools: ToolRegistry,
            system_prompt: str = DEFAULT_SYSTEM_PROMPT,
            event_handler: EventHandler | None = None
        ) -> AgentState:
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.event_handler = event_handler
        self.state = AgentState()

    def emit(self, event: Event) -> None:
        if self.event_handler is not None:
            self.event_handler(event)