"""Layered project/builtin skill discovery, conflicts and relative resources."""

import asyncio
from pathlib import Path

from cairn.infra.skills import SkillLibrary, layered_skills, resolve_skills
from cairn.infra.tools.skill import ReadSkillResourceTool

VALID = "---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n"


def write_skill(root: Path, directory: str, name: str, desc: str, body: str) -> None:
    target = root / directory
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        VALID.format(name=name, desc=desc, body=body), encoding="utf-8"
    )


def project_root(ws: Path) -> Path:
    return ws / ".cairn" / "skills"


def test_project_skill_shadows_builtin_deterministically(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    write_skill(root, "diagrams", "diagrams", "Project diagrams", "PROJECT BODY")
    write_skill(root, "local", "local", "Local helper", "LOCAL BODY")
    library = layered_skills(tmp_path)

    names = [item.name for item in library.summaries()]
    assert "local" in names
    diagrams = library.read("diagrams")
    assert diagrams is not None
    assert diagrams.source == "project"
    assert "PROJECT BODY" in diagrams.body
    assert any("shadowed" in item for item in library.conflicts())


def test_duplicate_names_in_one_layer_first_directory_wins(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    write_skill(root, "a", "dup", "first", "FIRST")
    write_skill(root, "b", "dup", "second", "SECOND")

    library = SkillLibrary(root)
    summaries = [item.name for item in library.summaries()]

    assert summaries.count("dup") == 1
    duplicate = library.read("dup")
    assert duplicate is not None
    assert "FIRST" in duplicate.body
    assert any("duplicate" in item for item in library.conflicts())


def test_resolve_selected_reports_missing(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    write_skill(root, "local", "local", "Local helper", "BODY")
    found, missing = resolve_skills(layered_skills(tmp_path), ["local", "nope"])

    assert [skill.name for skill in found] == ["local"]
    assert missing == ["nope"]


def test_relative_resource_is_read_and_escape_rejected(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    write_skill(root, "local", "local", "Local helper", "BODY")
    resource = root / "local" / "resources"
    resource.mkdir(parents=True)
    (resource / "ref.txt").write_text("reference text", encoding="utf-8")
    tool = ReadSkillResourceTool(layered_skills(tmp_path))

    ok = asyncio.run(tool.execute({"skill": "local", "path": "resources/ref.txt"}))
    assert ok.exit_code == 0
    assert ok.stdout == "reference text"

    escape = asyncio.run(tool.execute({"skill": "local", "path": "../SKILL.md"}))
    assert escape.exit_code == 1
    assert "Skill resource error" in escape.stderr


def test_invalid_project_skill_is_skipped(tmp_path: Path) -> None:
    root = project_root(tmp_path)
    target = root / "broken"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("---\nname: broken\n---\nbody\n", encoding="utf-8")
    library = layered_skills(tmp_path)

    assert "broken" not in [item.name for item in library.summaries()]
