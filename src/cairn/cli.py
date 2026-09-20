import asyncio
import os

from dotenv import load_dotenv
from pathlib import Path

from cairn.llm.litellm_client import LiteLLMClient
from cairn.ui import (
    print_banner, 
    console_event_handler,
    print_assistant_response,
)
from cairn.core.loop import run_turn
from cairn.core.agent import Agent
from cairn.tools.registry import ToolRegistry
from cairn.tools.bash import BashTool

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

    print_banner()

    registry = ToolRegistry()

    registry.register_tool(
        BashTool(
            cwd=Path.cwd()
        )
    )

    agent=Agent(
        llm=LiteLLMClient(
            model=model, 
            api_key=api_key, 
            api_base=base_url
        ),
        tools=registry,
        event_handler=console_event_handler
    )

    while True:
        user_input = input("cairn> ").strip()

        if user_input.lower() in ['/exit', '/quit']:
            print("Goodbye! see you next time.")
            break

        response = await run_turn(
            agent,
            user_input=user_input,
        )

        print_assistant_response(response)

if __name__ == "__main__":
    asyncio.run(main())