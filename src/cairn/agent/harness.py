from collections.abc import Callable

from cairn.agent.budget import BudgetUsage
from cairn.agent.context.compaction import ReusableCompaction
from cairn.agent.context.compaction.summarizer import Summarizer
from cairn.agent.context.git import RepoContextProvider
from cairn.agent.context.window import ContextBuilder
from cairn.agent.models.client import LLMClient
from cairn.agent.models.executor import ModelExecutor
from cairn.agent.permissions import PermissionHandler
from cairn.agent.prompts import BASE_SYSTEM_PROMPT
from cairn.agent.skills import SkillLibrary
from cairn.agent.tools.registry import ToolRegistry
from cairn.agent.tracing.tracer import Tracer
from cairn.agent.turn.events import Event, EventHandler
from cairn.agent.turn.state import AgentState


class SessionAdmissionError(RuntimeError):
    """An invalid binding/gate was rejected before execution began."""


DEFAULT_SYSTEM_PROMPT = BASE_SYSTEM_PROMPT


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        personal_context: str = "",
        event_handler: EventHandler | None = None,
        permission_handler: PermissionHandler | None = None,
        tracer: Tracer | None = None,
        repo_context_provider: RepoContextProvider | None = None,
        context_builder: ContextBuilder | None = None,
        model_executor: ModelExecutor | None = None,
        provider_name: str | None = None,
        skill_library: SkillLibrary | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        #: Lower-trust personal data supplied as a separate user message, never
        #: merged into the system prompt or treated as policy.
        self.personal_context = personal_context
        self.event_handler = event_handler
        self.permission_handler = permission_handler
        self.tracer = tracer
        self.repo_context_provider = repo_context_provider
        self.skill_library = skill_library
        self.context_builder = (
            context_builder if context_builder is not None else ContextBuilder()
        )
        self.after_tools: Callable[[], str | None] | None = None
        self.state = AgentState()
        self.last_budget_usage: BudgetUsage | None = None
        self.model_executor = (
            model_executor if model_executor is not None else ModelExecutor()
        )
        # Trace-only provider identity. Provider-neutral agents (core
        # tests) leave it unset and record no provider attribute.
        self.provider_name = provider_name
        # Supplied by the composition root; ``None`` means this Agent does not
        # compact. The Agent never builds one, so a caller that needs compaction
        # switched off (a scripted test client, an eval) says so explicitly.
        self.summarizer = summarizer
        # The last accepted compaction, reused while the history prefix it
        # replaced is unchanged. Compaction never edits history, so dropping this
        # value costs one repeated summary and nothing else.
        self.compaction: ReusableCompaction | None = None
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
