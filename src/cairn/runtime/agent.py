from collections.abc import Callable
from typing import TYPE_CHECKING

from cairn.core.context import ContextBuilder
from cairn.core.events import Event, EventHandler
from cairn.core.llm import LLMClient
from cairn.core.permissions import PermissionHandler
from cairn.core.state import AgentState
from cairn.infra.git.context import RepoContextProvider
from cairn.infra.observability.tracer import Tracer
from cairn.infra.skills import SkillLibrary
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.model_executor import ModelExecutor

if TYPE_CHECKING:
    from cairn.runtime.memory.retrieval import MemoryReader


class SessionAdmissionError(RuntimeError):
    """An invalid binding/gate was rejected before execution began."""


DEFAULT_SYSTEM_PROMPT = """You are Cairn, a software engineering agent.

When working on code:
1. Inspect before modifying.
   - Prioritize understanding existing tests before making code changes.
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
        repo_context_provider: RepoContextProvider | None = None,
        context_builder: ContextBuilder | None = None,
        model_executor: ModelExecutor | None = None,
        provider_name: str | None = None,
        memory_reader: "MemoryReader | None" = None,
        skill_library: SkillLibrary | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.event_handler = event_handler
        self.permission_handler = permission_handler
        self.tracer = tracer
        self.repo_context_provider = repo_context_provider
        self.skill_library = skill_library
        self.context_builder = (
            context_builder if context_builder is not None else ContextBuilder()
        )
        self.state = AgentState()
        self.model_executor = (
            model_executor if model_executor is not None else ModelExecutor()
        )
        # Trace-only provider identity. Provider-neutral agents (evals, core
        # tests) leave it unset and record no provider attribute.
        self.provider_name = provider_name
        if (
            memory_reader is not None
            and repo_context_provider is not None
            and memory_reader.resolver.workspace.root
            != repo_context_provider.workspace.root
        ):
            raise ValueError("Memory reader must use the Agent Workspace")
        self.memory_reader = memory_reader
        self._session_owner: object | None = None
        self._session_attribution: Callable[[Event], Event] | None = None
        # Only the loop advances this count. It prevents attachment midway
        # through an unbound turn without changing stateless execution policy.
        self._stateless_turns = 0

    @property
    def session_owner(self) -> object | None:
        return self._session_owner

    def _bind_session(
        self, session: object, attribute_event: Callable[[Event], Event]
    ) -> None:
        if self._session_owner is not None or self._stateless_turns:
            raise SessionAdmissionError("Agent is already owned or executing.")
        self._session_owner = session
        self._session_attribution = attribute_event

    def emit(self, event: Event) -> None:
        if self._session_owner is not None:
            if self._session_attribution is None:
                raise SessionAdmissionError("Session attachment is incomplete.")
            event = self._session_attribution(event)
        if self.event_handler is not None:
            self.event_handler(event)
