import asyncio
import os

from dotenv import load_dotenv

from cairn.llm.litellm_client import LiteLLMClient
from cairn.ui import print_banner
from cairn.loop import run_turn
from cairn.agent import Agent

async def main():
    load_dotenv()

    print_banner()

    model = os.getenv("CAIRN_LLM_MODEL")
    if not model:
        raise ValueError("CAIRN_LLM_MODEL environment variable is not set.")

    api_key = os.getenv("CAIRN_LLM_API_KEY")
    if not api_key:
        raise ValueError("CAIRN_LLM_API_KEY environment variable is not set.")

    base_url = os.getenv("CAIRN_BASE_URL")
    if not base_url:
        raise ValueError("CAIRN_BASE_URL environment variable is not set.")

    agent=Agent(
        llm=LiteLLMClient(
            model=model, 
            api_key=api_key, 
            api_base=base_url
        )
    )

    while True:
        user_input = input("cairn> ").strip()

        if user_input.lower() in ['/exit', '/quit']:
            break

        response = await run_turn(
            agent,
            user_input=user_input
        )

        print(f"Cairn> {response}\n")

if __name__ == "__main__":
    asyncio.run(main())