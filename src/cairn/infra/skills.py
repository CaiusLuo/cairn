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

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from cairn.infra.credential_paths import is_credential_path

SKILL_FILE = "SKILL.md"
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_BODY_BYTES = 64 * 1024
MAX_RESOURCE_BYTES = 256 * 1024


@dataclass(frozen=True)
class Skill:
    """One discovered skill: enough to advertise it and to read its body."""

    name: str
    description: str
    body: str
    #: The skill directory, so relative resource files can be resolved. None for
    #: a parsed value that did not come from a directory.
    directory: Path | None = None
    #: "project" or "builtin"; used only for diagnostics and precedence.
    source: str = "builtin"


@dataclass(frozen=True)
class SkillSummary:
    """The advertisement for one skill; carries no body."""

    name: str
    description: str


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Read standard safe YAML, including folded/literal descriptions."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            try:
                fields = yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError:
                return {}, text
            if not isinstance(fields, dict):
                return {}, text
            if any(
                key in fields and not isinstance(fields[key], str)
                for key in ("name", "description")
            ):
                return {}, text
            return {
                key: fields[key] for key in ("name", "description") if key in fields
            }, "\n".join(lines[index + 1 :]).strip("\n")
    return {}, text


def _read_file(root: Path, relative: Path, *, limit: int) -> str:
    """Bounded regular-file read through directory FDs; never follow symlinks."""
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("resource path must be relative to the skill directory")
    if is_credential_path(root / relative):
        raise PermissionError("reading credential material requires approval")
    # Anchor at the filesystem root, walking every component without symlinks.
    # Checking path.resolve() before opening would leave a replacement race.
    absolute = root.absolute()
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in (*absolute.parts[1:], *relative.parts[:-1]):
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        child = os.open(
            relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd
        )
        with os.fdopen(child, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("skill resource must be a regular file")
            if info.st_size > limit:
                raise ValueError("skill file exceeds the size limit")
            data = handle.read(limit + 1)
            if len(data) > limit:
                raise ValueError("skill file exceeds the size limit")
            return data.decode("utf-8")
    finally:
        os.close(fd)


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
    if len(name) > MAX_NAME_LENGTH or re.fullmatch(r"[\w-]+", name) is None:
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

    def __init__(self, root: Path | None, *, source: str = "project") -> None:
        self.root = root
        self.source = source
        self._conflicts: tuple[str, ...] = ()

    def conflicts(self) -> tuple[str, ...]:
        """Deterministic diagnostics from the last discovery pass."""
        return self._conflicts

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
        seen: dict[str, Path] = {}
        conflicts: list[str] = []
        for entry in sorted(root.iterdir()):
            path = entry / SKILL_FILE
            if not path.is_file():
                continue
            try:
                text = _read_file(
                    root, Path(entry.name) / SKILL_FILE, limit=MAX_BODY_BYTES + 16384
                )
            except (OSError, ValueError):
                # An unreadable skill is skipped: discovery must not fail a turn.
                continue
            parsed = parse_skill(text, fallback_name=entry.name)
            if parsed is None:
                continue
            if parsed.name in seen:
                conflicts.append(
                    f"duplicate skill '{parsed.name}' at {entry} ignored; "
                    f"first definition wins ({seen[parsed.name]})"
                )
                continue
            seen[parsed.name] = entry
            skills.append(
                Skill(
                    name=parsed.name,
                    description=parsed.description,
                    body=parsed.body,
                    directory=entry,
                    source=self.source,
                )
            )
        self._conflicts = tuple(conflicts)
        return skills

    def read_resource(self, name: str, relative_path: str) -> str:
        """Read a UTF-8 file named relative to one skill's directory.

        Only paths inside the skill directory are allowed; ``..`` and absolute
        paths are rejected, and the file is size-bounded. Relative resource files
        referenced by a SKILL.md therefore work without exposing the rest of the
        filesystem.
        """
        skill = self.read(name)
        if skill is None:
            raise KeyError(f"unknown skill: {name}")
        if skill.directory is None:
            raise ValueError(f"skill '{name}' has no directory")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("resource path must be relative to the skill directory")
        return _read_file(skill.directory, relative, limit=MAX_RESOURCE_BYTES)

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
    """The skills shipped with Cairn itself."""
    root = Path(__file__).resolve().parent.parent / "skills"
    return SkillLibrary(root, source="builtin")


def project_skills(workspace_root: Path) -> SkillLibrary:
    """The project's own skills under ``<workspace>/.cairn/skills``."""
    return SkillLibrary(workspace_root / ".cairn" / "skills", source="project")


class LayeredSkillLibrary(SkillLibrary):
    """Project skills layered over the built-in library, first layer wins.

    Precedence is deterministic: a project skill shadows a built-in skill of the
    same name, and ``conflicts()`` reports every shadowed name.
    """

    def __init__(self, layers: Sequence[tuple[str, SkillLibrary]]) -> None:
        super().__init__(None, source="layered")
        self.layers = tuple(layers)

    def summaries(self) -> tuple[SkillSummary, ...]:
        seen: set[str] = set()
        ordered: list[SkillSummary] = []
        for _label, library in self.layers:
            for item in library.summaries():
                if item.name in seen:
                    continue
                seen.add(item.name)
                ordered.append(item)
        return tuple(ordered)

    def read(self, name: str) -> Skill | None:
        for _label, library in self.layers:
            skill = library.read(name)
            if skill is not None:
                return skill
        return None

    def read_resource(self, name: str, relative_path: str) -> str:
        for _label, library in self.layers:
            skill = library.read(name)
            if skill is not None:
                return library.read_resource(name, relative_path)
        raise KeyError(f"unknown skill: {name}")

    def conflicts(self) -> tuple[str, ...]:
        diagnostics: list[str] = []
        for label, library in self.layers:
            diagnostics.extend(f"{label}: {item}" for item in library.conflicts())
        owner: dict[str, str] = {}
        for label, library in self.layers:
            for item in library.summaries():
                if item.name in owner:
                    diagnostics.append(
                        f"skill '{item.name}' from {label} shadowed by {owner[item.name]}"
                    )
                else:
                    owner[item.name] = label
        return tuple(diagnostics)


def resolve_skills(
    library: SkillLibrary, names: Sequence[str]
) -> tuple[list[Skill], list[str]]:
    """Resolve selected skill names into skills and missing names, in order."""
    found: list[Skill] = []
    missing: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        skill = library.read(name)
        if skill is None:
            missing.append(name)
        else:
            found.append(skill)
    return found, missing


def layered_skills(
    workspace_root: Path | None, *, builtin: SkillLibrary | None = None
) -> SkillLibrary:
    """Project skills over built-ins; a missing project directory is empty."""
    base = builtin if builtin is not None else builtin_skills()
    if workspace_root is None:
        return base
    return LayeredSkillLibrary(
        [("project", project_skills(workspace_root)), ("builtin", base)]
    )
