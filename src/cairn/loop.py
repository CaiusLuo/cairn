import json

from cairn.agent import Agent
from cairn.models import Message

MAX_STEPS = 20

async def run_turn(
        agent: Agent, 
        user_input: str
    ) -> str:
    agent.state.add_user_message(user_input)

    for step in range(MAX_STEPS):

        print(f"[step] {step + 1}/ {MAX_STEPS}")

        messages = [
            Message(
                role="system", 
                content=agent.system_prompt
            ),
            *agent.state.messages
        ]

        response = await agent.llm.generate(
            messages,
            tools=agent.tools.schemas(),
        )

        agent.state.add_assistant_message(
            response.content,
            tool_calls=response.tool_calls,
        )

        if not response.tool_calls:
            return response.content or ""

        for tool_call in response.tool_calls:
            print(
                f"[tool] {tool_call.name} "
                f"{tool_call.arguments}"
            )

            result = await agent.tools.execute(
                name=tool_call.name,
                arguments=tool_call.arguments,
            )

            print(
                f"[result] exit_code={result.exit_code} "
            )

            tool_content = json.dumps(
                result.model_dump(),
                ensure_ascii=False,
            )

            agent.state.add_tool_message(
                tool_call_id=tool_call.id,
                content=tool_content,
            )

    raise RuntimeError(
        f"Agent exceeded maximum steps: {MAX_STEPS}"  
    )
