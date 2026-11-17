"""Channel principal isolation: role comes from the connection, not the text."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from cairn.agent.harness import Agent
from cairn.agent.tools.base import ToolExecutionContext
from cairn.agent.tools.registry import ToolRegistry
from cairn.development.catalogue import ChannelPrincipal, RuntimeTask
from cairn.development.execution.runtime import apply_channel_tool_scope
from cairn.knowledge.store import KnowledgeStore
from cairn.server.config import Services
from cairn.server.management import ServerControls


class DummyTool:
    name = "bash"
    description = "test host tool"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        return None

    async def execute(
        self,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> Any:
        raise AssertionError("the test tool is never executed")


def principal(role: str) -> ChannelPrincipal:
    return ChannelPrincipal(
        connection_id="ch-1",
        platform="telegram",
        sender_id="7",
        role=role,  # type: ignore[arg-type]
        conversation_binding="7",
    )


def channel_task(role: str | None = None) -> RuntimeTask:
    return RuntimeTask(
        prompt="hello",
        source="channel",
        channel_principal=None if role is None else principal(role),
    )


def agent_with_registry() -> tuple[Agent, ToolRegistry]:
    registry = ToolRegistry()
    registry.register_tool(DummyTool())
    agent = cast(
        Agent,
        SimpleNamespace(
            tools=registry,
            personal_context="",
            skill_library=None,
        ),
    )
    return agent, registry


def controls() -> ServerControls:
    controls = ServerControls.__new__(ServerControls)
    controls.services = Services(memory=True, schedule=False)
    controls.workspace = Path("/tmp/cairn-principal-test")
    return controls


def test_allowed_sender_loses_every_host_tool() -> None:
    agent, registry = agent_with_registry()
    task = channel_task("allowed")
    apply_channel_tool_scope(agent, task)
    assert dict(registry.tools) == {}


def test_owner_and_direct_runs_keep_their_tools() -> None:
    owner_agent, owner_registry = agent_with_registry()
    apply_channel_tool_scope(owner_agent, channel_task("owner"))
    assert "bash" in owner_registry.tools

    direct_agent, direct_registry = agent_with_registry()
    apply_channel_tool_scope(direct_agent, channel_task())
    assert "bash" in direct_registry.tools


def test_allowed_sender_gets_no_personal_memory_or_skills() -> None:
    agent, registry = agent_with_registry()
    task = channel_task("allowed")
    # ``services.memory`` is enabled but the allowed principal must not reach
    # the host's memory service at all.
    controls().configure_agent(agent, task)
    assert agent.personal_context == ""
    assert agent.skill_library is None
    assert "read_skill" not in registry.tools
    assert "bash" in registry.tools  # removed later by the execution gate


def test_owner_gets_skills_but_direct_controls_are_unused_for_guests(
    tmp_path: Path,
) -> None:
    agent, registry = agent_with_registry()
    owner = controls()
    owner.services = Services(memory=False, schedule=False)
    owner.workspace = tmp_path
    owner.configure_agent(agent, channel_task("owner"))
    assert agent.skill_library is not None
    assert "read_skill" in registry.tools


def test_knowledge_tools_are_owner_only(tmp_path: Path) -> None:
    owner = controls()
    owner.services = Services(memory=False, schedule=False)
    owner.workspace = tmp_path
    owner.knowledge_store = KnowledgeStore(tmp_path / "knowledge")
    owner.knowledge_store.create("PRIVATE-KNOWLEDGE-ONLY")

    owner_agent, owner_registry = agent_with_registry()
    owner_agent.system_prompt = "Assistant."
    owner.configure_agent(owner_agent, channel_task("owner"))
    assert {"knowledge_list", "knowledge_search", "knowledge_get"} <= set(
        owner_registry.tools
    )
    assert "knowledge_list" in owner_agent.system_prompt

    guest_agent, guest_registry = agent_with_registry()
    owner.configure_agent(guest_agent, channel_task("allowed"))
    assert "knowledge_search" not in guest_registry.tools
    assert "knowledge_get" not in guest_registry.tools
    apply_channel_tool_scope(guest_agent, channel_task("allowed"))
    assert not guest_registry.tools
