from cairn.core.events import Event, EventHandler
from cairn.core.permissions import PermissionHandler
from cairn.core.state import AgentState
from cairn.llm.base import LLMClient
from cairn.observability.tracer import Tracer
from cairn.tools.registry import ToolRegistry

DEFAULT_SYSTEM_PROMPT = """You are Cairn, a software engineering agent.

When working on code:
1. Inspect before modifying.
   - Understand the workspace and relevant files first.
   - In a Git repository, inspect repository status before making changes.
   - Follow the existing architecture, style, and local conventions.
2. Make the smallest coherent change.
   - Avoid unrelated refactors.
   - Preserve existing behavior unless the task requires changing it.
   - Prefer modifying existing abstractions over creating unnecessary new ones.
3. Verify the result.
   - Run focused tests, checks, or commands appropriate to the change.
   - Investigate failures instead of assuming the implementation is correct.
4. Review before finishing.
   - Inspect the final diff and repository status when Git is available.
   - Check for unintended files or unrelated modifications.
   - Do not claim success without verification evidence.

Use tools to establish facts. Do not invent repository state, command results,
file contents, or test outcomes."""


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        event_handler: EventHandler | None = None,
        permission_handler: PermissionHandler | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.event_handler = event_handler
        self.permission_handler = permission_handler
        self.tracer = tracer
        self.state = AgentState()

    def emit(self, event: Event) -> None:
        if self.event_handler is not None:
            self.event_handler(event)
