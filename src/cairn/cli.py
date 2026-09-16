import asyncio
import os

from dotenv import load_dotenv
from cairn.models import Message

from cairn.llm.litellm_client import LiteLLMClient
from cairn.ui import print_banner

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

    client = LiteLLMClient(
        model=model, 
        api_key=api_key, 
        api_base=base_url
    )

    while True:
        user_input = input("cairn> ").strip()

        if user_input.lower() in ['/exit', '/quit']:
            break

        messages = [
            Message(
                role="user", 
                content=user_input
            )
        ]

        response = await client.generate(messages=messages)

        print(f"Cairn> {response.content}\n")

if __name__ == "__main__":
    asyncio.run(main())