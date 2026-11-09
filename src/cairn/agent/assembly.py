from collections.abc import Sequence

from cairn.agent.context.compaction.summarizer import Summarizer
from cairn.agent.context.git import RepoContextProvider
from cairn.agent.context.window import ContextBuilder
from cairn.agent.harness import DEFAULT_SYSTEM_PROMPT, Agent
from cairn.agent.models.client import LLMClient
from cairn.agent.models.executor import ModelExecutor
from cairn.agent.models.providers.compaction_summarizer import LLMCompactionSummarizer
from cairn.agent.permissions import PermissionHandler
from cairn.agent.skills import SkillLibrary
from cairn.agent.tools.base import Tool
from cairn.agent.tools.defaults import build_default_registry
from cairn.agent.tracing.tracer import Tracer
from cairn.agent.turn.events import EventHandler
from cairn.agent.workspace.workspace import Workspace


def build_agent(
    *,
    workspace: Workspace,
    llm: LLMClient,
    permission_handler: PermissionHandler | None,
    event_handler: EventHandler | None,
    tracer: Tracer | None,
    context_builder: ContextBuilder | None = None,
    secret_env_keys: frozenset[str] = frozenset(),
    model_executor: ModelExecutor | None = None,
    skills: SkillLibrary | None = None,
    system_prompt: str | None = None,
    personal_context: str = "",
    extra_tools: Sequence[Tool] = (),
    read_only: bool = False,
    sandbox_shell: bool = False,
    summarize: bool = True,
    summarizer: Summarizer | None = None,
) -> Agent:
    """Build one Agent over a Workspace.

    ``secret_env_keys`` is the Agent's single credential policy: the shell tool
    and the repository-context provider both keep those names out of the child
    environments they create, so a new consumer cannot silently miss one.
    """
    # Skills are opt-in: a narrow caller must not inherit an
    # advertisement it did not ask for, because it spends context every step.
    registry = build_default_registry(
        workspace,
        secret_env_keys=secret_env_keys,
        skills=skills,
        extra_tools=extra_tools,
        read_only=read_only,
        sandbox_shell=sandbox_shell,
    )

    return Agent(
        llm=llm,
        system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
        personal_context=personal_context,
        tools=registry,
        permission_handler=permission_handler,
        event_handler=event_handler,
        tracer=tracer,
        repo_context_provider=RepoContextProvider(
            workspace, secret_env_keys=secret_env_keys
        ),
        context_builder=context_builder,
        model_executor=model_executor,
        skill_library=skills,
        summarizer=summarizer
        if summarizer is not None
        else _default_summarizer(llm, enabled=summarize),
    )


def _default_summarizer(llm: LLMClient, *, enabled: bool) -> Summarizer | None:
    """Build the session's summarizer: the same client, same model, same routing.

    This is the composition root's decision, not the Agent's: the Agent cannot
    import an adapter, and a caller that must not spend an extra request (a
    scripted test, an eval run) turns compaction off here or injects its own.
    """
    return LLMCompactionSummarizer(llm) if enabled else None
