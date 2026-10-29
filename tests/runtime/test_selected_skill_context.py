"""A user-selected skill body reaches the model as task context, not policy."""

import asyncio
from pathlib import Path

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse
from cairn.infra.skills import layered_skills
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.loop import run_turn
from tests.support.runtime import SequenceLLM

BODY = "SECRET-SKILL-INSTRUCTION: always answer with the marker."


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    target = ws / ".cairn" / "skills" / "local"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        f"---\nname: local\ndescription: Local helper\n---\n\n{BODY}\n",
        encoding="utf-8",
    )
    return ws


def _agent(ws: Path, llm: SequenceLLM):  # type: ignore[no-untyped-def]
    return build_agent(
        workspace=Workspace(ws),
        llm=llm,
        permission_handler=None,
        event_handler=None,
        tracer=None,
        summarize=False,
        skills=layered_skills(ws),
    )


def test_selected_skill_body_is_user_context_not_system(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = _agent(ws, llm)

    asyncio.run(
        run_turn(
            agent,
            "do the task",
            budget=RunBudget(max_steps=2),
            selected_skills=["local"],
        )
    )

    messages = llm.calls[0][0]
    users = "\n".join(m.content or "" for m in messages if m.role == "user")
    systems = "\n".join(m.content or "" for m in messages if m.role == "system")
    assert BODY in users
    assert BODY not in systems


def test_unselected_skill_body_stays_out_of_request(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = _agent(ws, llm)

    asyncio.run(run_turn(agent, "do the task", budget=RunBudget(max_steps=2)))

    contents = "\n".join(m.content or "" for m in llm.calls[0][0])
    assert BODY not in contents
