from typing import TYPE_CHECKING

from cairn.core.context import ContextBuilder
from cairn.core.events import EventHandler
from cairn.core.llm import LLMClient
from cairn.core.permissions import PermissionHandler
from cairn.infra.git.context import RepoContextProvider
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.bash import BashTool
from cairn.infra.tools.files import EditFileTool, ReadFileTool
from cairn.infra.tools.registry import ToolRegistry
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
    repository_secret_env_keys: frozenset[str] | None = None,
    memory_reader: "MemoryReader | None" = None,
) -> Agent:
    registry = ToolRegistry()

    registry.register_tool(BashTool(workspace, secret_env_keys=secret_env_keys))
    registry.register_tool(ReadFileTool(workspace))
    registry.register_tool(EditFileTool(workspace))

    return Agent(
        llm=llm,
        tools=registry,
        permission_handler=permission_handler,
        event_handler=event_handler,
        tracer=tracer,
        repo_context_provider=RepoContextProvider(
            workspace, secret_env_keys=repository_secret_env_keys
        ),
        context_builder=context_builder,
        model_executor=model_executor,
        memory_reader=memory_reader,
    )
