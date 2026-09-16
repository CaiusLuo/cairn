from cairn.llm.base import LLMClient
from cairn.state import AgentState
from cairn.tools.registry import ToolRegistry

DEFAULT_SYSTEM_PROMPT = """
You are Cairn, a personal agent.
"""

class Agent:
    def __init__(
            self,
            llm: LLMClient,
            tools: ToolRegistry,
            system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        ) -> AgentState:
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.state = AgentState()