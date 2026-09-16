from cairn.agent import Agent
from cairn.models import Message

async def run_turn(
        agent: Agent, 
        user_input: str
    ) -> str:
    agent.state.add_user_message(user_input)

    messages = [
        Message(
            role="system", 
            content=agent.system_prompt
        ),
        *agent.state.messages
    ]

    response = await agent.llm.generate(messages)

    agent.state.add_assistant_message(
        response.content
    )

    return response.content