"""Skill discovery advertises cheaply and reads bodies only on request."""

import asyncio
from pathlib import Path

import pytest

from cairn.core.budget import RunBudget
from cairn.core.events import Event
from cairn.core.models import LLMResponse, ToolCall
from cairn.infra.skills import SkillLibrary, builtin_skills, parse_skill
from cairn.infra.tools.base import InvalidArguments
from cairn.infra.tools.skill import ReadSkillTool
from cairn.runtime.loop import run_turn
from tests.support.runtime import RecordingTool, SequenceLLM, make_agent

VALID = """\
---
name: reporting
description: Write a status report for a stakeholder.
---

Report body.
"""


def write_skill(root: Path, directory: str, text: str) -> None:
    (root / directory).mkdir(parents=True, exist_ok=True)
    (root / directory / "SKILL.md").write_text(text, encoding="utf-8")


def test_missing_directory_advertises_nothing(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "absent")

    assert library.summaries() == ()
    assert library.prompt_lines() == []
    assert library.read("reporting") is None


def test_skill_is_advertised_without_its_body(tmp_path: Path) -> None:
    write_skill(tmp_path, "reporting", VALID)
    library = SkillLibrary(tmp_path)

    [summary] = library.summaries()

    assert summary.name == "reporting"
    assert summary.description == "Write a status report for a stakeholder."
    assert "Report body" not in repr(summary)
    skill = library.read("reporting")
    assert skill is not None
    assert skill.body.strip() == "Report body."


def test_prompt_lines_carry_name_and_description_only(tmp_path: Path) -> None:
    write_skill(tmp_path, "reporting", VALID)

    lines = SkillLibrary(tmp_path).prompt_lines()

    assert any("reporting: Write a status report" in line for line in lines)
    assert not any("Report body" in line for line in lines)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("no frontmatter at all", "missing header"),
        ("---\nname: x\n---\nbody\n", "no description"),
        ("---\nname: x\ndescription: d\n---\n\n", "empty body"),
        ("---\nname: x\ndescription: d\n", "unterminated header"),
    ],
)
def test_unusable_skills_are_skipped_not_advertised(text: str, reason: str) -> None:
    assert parse_skill(text, fallback_name="dir") is None, reason


def test_directory_name_is_the_fallback_name(tmp_path: Path) -> None:
    text = "---\ndescription: d\n---\nbody\n"
    write_skill(tmp_path, "from-directory", text)

    [summary] = SkillLibrary(tmp_path).summaries()

    assert summary.name == "from-directory"


def test_shipped_library_advertises_the_diagrams_skill() -> None:
    library = builtin_skills()

    names = [item.name for item in library.summaries()]

    assert "diagrams" in names
    shipped = library.read("diagrams")
    assert shipped is not None
    body = shipped.body
    # The body must state when to draw and when not to.
    assert "Do **not** draw" in body


def test_read_skill_tool_returns_the_body(tmp_path: Path) -> None:
    write_skill(tmp_path, "reporting", VALID)
    tool = ReadSkillTool(SkillLibrary(tmp_path))

    result = asyncio.run(tool.execute({"name": "reporting"}))

    assert result.exit_code == 0
    assert result.stdout.strip() == "Report body."


def test_read_skill_tool_lists_available_names_on_miss(tmp_path: Path) -> None:
    write_skill(tmp_path, "reporting", VALID)
    tool = ReadSkillTool(SkillLibrary(tmp_path))

    result = asyncio.run(tool.execute({"name": "absent"}))

    assert result.exit_code == 1
    assert "Unknown skill" in result.stderr
    assert "reporting" in result.stderr


def test_read_skill_tool_rejects_bad_arguments(tmp_path: Path) -> None:
    tool = ReadSkillTool(SkillLibrary(tmp_path))

    with pytest.raises(InvalidArguments):
        tool.validate({"name": "x", "extra": 1})
    with pytest.raises(InvalidArguments):
        tool.validate({"name": "   "})


def test_skill_index_is_injected_and_body_is_not(tmp_path: Path) -> None:
    """The system message advertises the index; the body stays out until read."""
    write_skill(tmp_path, "reporting", VALID)
    library = SkillLibrary(tmp_path)
    events: list[Event] = []
    tool = RecordingTool()
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = make_agent(llm, tool, events)
    agent.skill_library = library

    asyncio.run(run_turn(agent, "status please", budget=RunBudget(max_steps=2)))

    messages, _ = llm.calls[0]
    system_contents = "\n".join(
        message.content or "" for message in messages if message.role == "system"
    )
    assert "reporting: Write a status report" in system_contents
    assert "Report body" not in system_contents


def test_reading_a_skill_is_observable_as_a_tool_call(tmp_path: Path) -> None:
    """A skill read travels the ordinary tool events, so it is observable."""
    write_skill(tmp_path, "reporting", VALID)
    events: list[Event] = []
    tool = ReadSkillTool(SkillLibrary(tmp_path))
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="c1", name="read_skill", arguments={"name": "reporting"}
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    agent = make_agent(llm, tool, events)

    asyncio.run(run_turn(agent, "report", budget=RunBudget(max_steps=3)))

    calls = [e for e in events if e.type == "tool_call"]
    assert calls[0].data["tool"] == "read_skill"
    assert calls[0].data["arguments"] == {"name": "reporting"}
