from typing import TYPE_CHECKING

from cairn.core.context import ContextBuilder
from cairn.core.events import EventHandler
from cairn.core.llm import LLMClient
from cairn.core.permissions import PermissionHandler
from cairn.infra.git.context import RepoContextProvider
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.bash import BashTool
from cairn.infra.tools.files import EditFileTool, ReadFileTool
from cairn.infra.tools.glob import GlobTool
from cairn.infra.tools.grep import GrepTool
from cairn.infra.tools.patch import PatchTool
from cairn.infra.tools.registry import ToolRegistry
from cairn.infra.tools.write import WriteTool
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
) -> Agent:
    """Build one Agent over a Workspace.

    ``secret_env_keys`` is the Agent's single credential policy: the shell tool
    and the repository-context provider both keep those names out of the child
    environments they create, so a new consumer cannot silently miss one.
    """
    registry = ToolRegistry()

    registry.register_tool(BashTool(workspace, secret_env_keys=secret_env_keys))
    registry.register_tool(ReadFileTool(workspace))
    registry.register_tool(EditFileTool(workspace))
    registry.register_tool(WriteTool(workspace))
    registry.register_tool(PatchTool(workspace))
    registry.register_tool(GlobTool(workspace))
    registry.register_tool(GrepTool(workspace))

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
    )
