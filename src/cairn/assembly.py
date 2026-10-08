from cairn.core.agent import Agent
from cairn.core.context import ContextBuilder
from cairn.core.events import EventHandler
from cairn.core.permissions import PermissionHandler
from cairn.llm.base import LLMClient
from cairn.observability.tracer import Tracer
from cairn.repository import RepoContextProvider
from cairn.tools.bash import BashTool
from cairn.tools.files import EditFileTool, ReadFileTool
from cairn.tools.registry import ToolRegistry
from cairn.workspace.workspace import Workspace


def build_agent(
    *,
    workspace: Workspace,
    llm: LLMClient,
    permission_handler: PermissionHandler | None,
    event_handler: EventHandler | None,
    tracer: Tracer | None,
    context_builder: ContextBuilder | None = None,
    secret_env_keys: frozenset[str] = frozenset(),
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
        repo_context_provider=RepoContextProvider(workspace),
        context_builder=context_builder,
    )
