import asyncio
import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from cairn.commands.context import CommandContext
from cairn.commands.router import CommandRouter
from cairn.core.agent import Agent
from cairn.core.events import Event
from cairn.core.loop import run_turn
from cairn.observability.reader import JsonlTraceReader
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer
from cairn.tools.bash import BashTool
from cairn.tools.files import EditFileTool, ReadFileTool
from cairn.tools.registry import ToolRegistry
from cairn.ui import (
    console_event_handler,
    console_permission_handler,
    print_assistant_response,
    print_banner,
    print_runtime_error,
)

app = typer.Typer(
    invoke_without_command=True,
)


@app.callback()
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        asyncio.run(main())


async def main() -> None:
    load_dotenv()

    model = os.getenv("CAIRN_LLM_MODEL")
    if not model:
        raise ValueError("CAIRN_LLM_MODEL environment variable is not set.")

    api_key = os.getenv("CAIRN_LLM_API_KEY")
    if not api_key:
        raise ValueError("CAIRN_LLM_API_KEY environment variable is not set.")

    base_url = os.getenv("CAIRN_BASE_URL")
    if not base_url:
        raise ValueError("CAIRN_BASE_URL environment variable is not set.")

    from cairn.llm.litellm_client import LiteLLMClient

    print_banner()

    registry = ToolRegistry()

    registry.register_tool(BashTool(cwd=Path.cwd()))
    registry.register_tool(ReadFileTool(cwd=Path.cwd()))
    registry.register_tool(EditFileTool(cwd=Path.cwd()))

    trace_root = Path(".cairn/traces")
    tracer = Tracer(JsonlTraceSink(trace_root))
    command_context = CommandContext(trace_reader=JsonlTraceReader(trace_root))
    pending_trace_finish: Event | None = None

    def handle_event(event: Event) -> None:
        nonlocal pending_trace_finish
        if event.type == "trace_finish":
            command_context.last_trace_id = event.data["trace_id"]
            pending_trace_finish = event
            return
        console_event_handler(event)

    agent = Agent(
        llm=LiteLLMClient(model=model, api_key=api_key, api_base=base_url),
        tools=registry,
        event_handler=handle_event,
        permission_handler=console_permission_handler,
        tracer=tracer,
    )

    router = CommandRouter()

    while True:
        user_input = input("cairn> ").strip()
        if not user_input:
            continue

        command_result = router.handle(
            user_input,
            command_context,
        )

        if command_result.handled:
            if command_result.should_exit:
                print("Goodbye! see you next time.")
                break
            continue

        try:
            response = await run_turn(agent, user_input=user_input)
        except Exception as exc:
            print_runtime_error(exc)
        else:
            print_assistant_response(response)
        finally:
            if pending_trace_finish is not None:
                console_event_handler(pending_trace_finish)
                pending_trace_finish = None


if __name__ == "__main__":
    app()
