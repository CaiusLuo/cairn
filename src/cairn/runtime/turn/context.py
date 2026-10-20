"""The system context of one request, in the order the model receives it.

A request opens with the Agent's system prompt and then each further system
message its configuration contributes: a durable recovery notice, the repository
context and the advertised skills. Both the live loop and the manual compaction
entry build that prefix here, so compaction measures the request the model
actually receives rather than a smaller one assembled from a subset.

This module only assembles messages. ``run_turn`` still owns the turn, and the
compaction hook still owns when a reduction runs.
"""

from cairn.core.models import Message
from cairn.runtime.agent import Agent
from cairn.runtime.durable_session import DurableSession


async def build_system_messages(agent: Agent) -> list[Message]:
    """The system messages the next request carries, in provider order.

    Each contributing capability is read once: the durable notice is a property
    over the Session's last recovery report, and the repository context is one
    bounded inspection, never a second one for the same request.
    """
    messages = [Message(role="system", content=agent.system_prompt)]
    durable = _owning_durable_session(agent)
    notice = None if durable is None else durable.recovery_notice
    if notice is not None:
        messages.append(Message(role="system", content=notice))
    if agent.repo_context_provider is not None:
        repo_context = await agent.repo_context_provider.inspect()
        messages.append(Message(role="system", content=repo_context.to_prompt()))
    if agent.skill_library is not None:
        # Advertise names only; each body arrives through read_skill, so a large
        # library costs nothing until a skill is used.
        skill_lines = agent.skill_library.prompt_lines()
        if skill_lines:
            messages.append(Message(role="system", content="\n".join(skill_lines)))
    return messages


def _owning_durable_session(agent: Agent) -> DurableSession | None:
    """The durable Session that owns this Agent, when one does.

    A manual compaction is asked to measure the next request, and a durable
    recovery notice belongs to that request. The Session is already the Agent's
    one owner, so read the notice from it instead of asking the command caller to
    pass the same binding again.
    """
    owner = agent.session_owner
    return owner if isinstance(owner, DurableSession) else None
