from cairn.core.agent import Agent
from cairn.core.events import EventHandler
from cairn.core.permissions import PermissionHandler
from cairn.llm.base import LLMClient
from cairn.observability.tracer import Tracer
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
) -> Agent:
    registry = ToolRegistry()

    registry.register_tool(BashTool(workspace))
    registry.register_tool(ReadFileTool(workspace))
    registry.register_tool(EditFileTool(workspace))

    return Agent(
        llm=llm,
        tools=registry,
        permission_handler=permission_handler,
        event_handler=event_handler,
        tracer=tracer,
    )
