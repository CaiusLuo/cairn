import asyncio
import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from cairn.commands.trace import trace_app
from cairn.core.agent import Agent
from cairn.core.loop import run_turn
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer
from cairn.tools.bash import BashTool
from cairn.tools.registry import ToolRegistry
from cairn.ui import (
    console_event_handler,
    console_permission_handler,
    print_assistant_response,
    print_banner,
)

app = typer.Typer(
    invoke_without_command=True,
)

app.add_typer(trace_app, name="trace")


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

    tracer = Tracer(JsonlTraceSink(Path(".cairn/traces")))

    agent = Agent(
        llm=LiteLLMClient(model=model, api_key=api_key, api_base=base_url),
        tools=registry,
        event_handler=console_event_handler,
        permission_handler=console_permission_handler,
        tracer=tracer,
    )

    while True:
        user_input = input("cairn> ").strip()

        if user_input.lower() in ["/exit", "/quit"]:
            print("Goodbye! see you next time.")
            break

        response = await run_turn(
            agent,
            user_input=user_input,
        )

        print_assistant_response(response)


if __name__ == "__main__":
    app()
