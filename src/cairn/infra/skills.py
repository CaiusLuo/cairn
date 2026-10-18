"""Discover and read agent skills from a project directory.

A skill is a directory holding ``SKILL.md``: YAML frontmatter with a ``name`` and
a ``description``, then a Markdown body. Only those two frontmatter fields are
read; anything else is ignored so a skill written for another harness loads here
unchanged.

Discovery is deliberately cheap: callers inject one line per skill so the model
knows a skill exists, and the body is read only when the model asks for it. That
keeps a large skill library out of the prompt until it is used.

This module performs no model calls, grants no permission and writes nothing. A
missing directory is not an error: a project without skills simply has none.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SKILL_FILE = "SKILL.md"
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class Skill:
    """One discovered skill: enough to advertise it and to read its body."""

    name: str
    description: str
    body: str


@dataclass(frozen=True)
class SkillSummary:
    """The advertisement for one skill; carries no body."""

    name: str
    description: str


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split leading YAML frontmatter from the body.

    Only flat ``key: value`` pairs are read, which is all a skill needs. A file
    without frontmatter is treated as all body.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if len(lines) < 2 or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body = "\n".join(lines[index + 1 :]).strip("\n")
            return fields, body
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("\"'")
    # An unterminated block is malformed frontmatter, not a skill header.
    return {}, text


def _single_line(value: str) -> str:
    return " ".join(value.split())


def parse_skill(text: str, *, fallback_name: str = "") -> Skill | None:
    """Build a Skill from one file's text, or None when it is unusable.

    A skill needs a usable name and a non-empty description; without a
    description the model has nothing to match against, so it is skipped rather
    than advertised blank.
    """
    fields, body = _frontmatter(text)
    name = _single_line(fields.get("name") or fallback_name)
    description = _single_line(fields.get("description", ""))
    if not name or len(name) > MAX_NAME_LENGTH:
        return None
    if not description or len(description) > MAX_DESCRIPTION_LENGTH:
        return None
    if not body.strip():
        return None
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        return None
    return Skill(name=name, description=description, body=body)


class SkillLibrary:
    """The skills available under one directory, read on demand."""

    def __init__(self, root: Path | None) -> None:
        self.root = root

    def summaries(self) -> tuple[SkillSummary, ...]:
        """Advertisements for every usable skill, in directory-name order."""
        return tuple(
            SkillSummary(name=skill.name, description=skill.description)
            for skill in self._load_all()
        )

    def read(self, name: str) -> Skill | None:
        """The skill called ``name``, or None when no skill has that name."""
        if not isinstance(name, str) or not name:
            return None
        for skill in self._load_all():
            if skill.name == name:
                return skill
        return None

    def _load_all(self) -> list[Skill]:
        root = self.root
        if root is None or not root.is_dir():
            return []
        skills: list[Skill] = []
        for entry in sorted(root.iterdir()):
            path = entry / SKILL_FILE
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                # An unreadable skill is skipped: discovery must not fail a turn.
                continue
            skill = parse_skill(text, fallback_name=entry.name)
            if skill is not None:
                skills.append(skill)
        return skills

    def prompt_lines(self) -> list[str]:
        """One line per skill, for a system message; empty when there are none."""
        summaries = self.summaries()
        if not summaries:
            return []
        lines = [
            "Skills you can load on demand. Read one with the read_skill tool "
            "only when it would change what you do:",
        ]
        lines.extend(f"- {item.name}: {item.description}" for item in summaries)
        return lines


def builtin_skills() -> SkillLibrary:
    """The skills shipped with Cairn itself.

    A user directory can be layered on later; today the package ships the
    default library so a fresh install has something to advertise.
    """
    root = Path(__file__).resolve().parent.parent / "skills"
    return SkillLibrary(root)
