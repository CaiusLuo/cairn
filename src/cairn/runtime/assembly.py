from collections.abc import Sequence
from typing import TYPE_CHECKING

from cairn.core.compaction.summarizer import Summarizer
from cairn.core.context import ContextBuilder
from cairn.core.events import EventHandler
from cairn.core.llm import LLMClient
from cairn.core.permissions import PermissionHandler
from cairn.infra.git.context import RepoContextProvider
from cairn.infra.llm.compaction_summarizer import LLMCompactionSummarizer
from cairn.infra.observability.tracer import Tracer
from cairn.infra.skills import SkillLibrary
from cairn.infra.tools import build_default_registry
from cairn.infra.tools.base import Tool
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.model_executor import ModelExecutor

if TYPE_CHECKING:
    from cairn.runtime.memory.retrieval import MemoryReader


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
    memory_reader: "MemoryReader | None" = None,
    skills: SkillLibrary | None = None,
    extra_tools: Sequence[Tool] = (),
    summarize: bool = True,
    summarizer: Summarizer | None = None,
) -> Agent:
    """Build one Agent over a Workspace.

    ``secret_env_keys`` is the Agent's single credential policy: the shell tool
    and the repository-context provider both keep those names out of the child
    environments they create, so a new consumer cannot silently miss one.
    """
    # Skills are opt-in: an evaluation or a narrow caller must not inherit an
    # advertisement it did not ask for, because it spends context every step.
    registry = build_default_registry(
        workspace,
        secret_env_keys=secret_env_keys,
        skills=skills,
        extra_tools=extra_tools,
    )

    return Agent(
        llm=llm,
        tools=registry,
        permission_handler=permission_handler,
        event_handler=event_handler,
        tracer=tracer,
        repo_context_provider=RepoContextProvider(
            workspace, secret_env_keys=secret_env_keys
        ),
        context_builder=context_builder,
        model_executor=model_executor,
        memory_reader=memory_reader,
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
