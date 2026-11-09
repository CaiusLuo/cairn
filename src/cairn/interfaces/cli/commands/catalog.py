"""One catalogue for root commands, subcommands and help.

The palette and ``/help`` both read this module; neither owns a second list. A
caller can look up a command path, list children, or render a help block through
the same small interface.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandEntry:
    name: str
    summary: str
    usage: str = ""
    detail: str = ""
    children: tuple[CommandEntry, ...] = ()


def _child(name: str, summary: str) -> CommandEntry:
    return CommandEntry(name=name, summary=summary, usage=name)


class CommandCatalog:
    def __init__(self, roots: Sequence[CommandEntry]) -> None:
        self.roots = tuple(roots)

    def root_rows(self) -> tuple[tuple[str, str], ...]:
        return tuple((entry.name, entry.summary) for entry in self.roots)

    def children(self, name: str) -> tuple[tuple[str, str], ...]:
        entry = self.find((name,))
        if entry is None:
            return ()
        return tuple((child.name, child.summary) for child in entry.children)

    def children_map(self) -> dict[str, tuple[tuple[str, str], ...]]:
        return {
            entry.name: tuple((child.name, child.summary) for child in entry.children)
            for entry in self.roots
            if entry.children
        }

    def find(self, path: Sequence[str]) -> CommandEntry | None:
        if not path:
            return None
        head = path[0].lstrip("/")
        current = next(
            (entry for entry in self.roots if entry.name.lstrip("/") == head), None
        )
        for part in path[1:]:
            if current is None:
                return None
            current = next(
                (child for child in current.children if child.name == part), None
            )
        return current

    def help_text(self, path: Sequence[str]) -> str | None:
        entry = self.find(path)
        if entry is None:
            return None
        lines = [f"Usage: {entry.usage or entry.name}", entry.detail or entry.summary]
        if entry.children:
            lines.append("Subcommands:")
            lines.extend(
                f"  {child.name:<12} {child.summary}" for child in entry.children
            )
        return "\n".join(lines)


def _root(
    name: str,
    summary: str,
    *children: tuple[str, str],
    usage: str = "",
    detail: str = "",
) -> CommandEntry:
    return CommandEntry(
        name=name,
        summary=summary,
        usage=usage or name,
        detail=detail,
        children=tuple(_child(child, summary) for child, summary in children),
    )


CATALOG = CommandCatalog(
    (
        _root(
            "/model",
            "Choose the active model group",
            ("use", "Select a model group or ID"),
            ("list", "Show configured model IDs"),
            ("add", "Append a model ID"),
            ("remove", "Delete a model ID"),
            ("move", "Reorder a model ID"),
        ),
        _root(
            "/provider",
            "Switch the conversation provider",
            ("use", "Switch provider"),
            ("list", "Show configured providers"),
        ),
        _root("/compact", "Reduce conversation context"),
        _root(
            "/trace",
            "Inspect stored execution details",
            ("list", "Show recent completed traces"),
            ("count", "Count stored traces"),
            ("del", "Delete a trace or oldest traces"),
        ),
        _root(
            "/help",
            "Show command help",
            usage="/help [COMMAND [SUBCOMMAND]]",
            detail="Show the command catalogue or one command's help.",
        ),
        _root("/exit", "Exit Cairn"),
        _root(
            "/wx",
            "WeChat login and task service",
            ("help", "List WeChat commands"),
            ("login", "Start a fresh QR login"),
            ("status", "Show login and service state"),
            ("verify", "Submit a phone verification code"),
            ("allow", "Add a trusted sender"),
            ("stop", "Stop the service but keep the account"),
            ("disconnect", "Stop and clear the local account for a fresh scan"),
        ),
        _root(
            "/schedule",
            "Manage scheduled tasks",
            ("list", "Show scheduled tasks"),
            ("history", "Show recent run history"),
            ("add", "Create a scheduled task"),
            ("pause", "Pause a task"),
            ("resume", "Resume a task"),
            ("remove", "Remove a task"),
        ),
        _root(
            "/task",
            "Shared task board and todos",
            ("list", "List shared tasks"),
            ("show", "Show one task and its todos"),
            ("add", "Create a local task"),
            ("use", "Select the current task"),
            ("todo", "Add a todo"),
            ("done", "Mark a todo done"),
            ("undo", "Mark a todo undone"),
            ("status", "Set task status"),
            ("progress", "Append a progress note"),
        ),
        _root(
            "/memory",
            "Personal AGENT.md and daily memory",
            ("show", "Show memory paths and sizes"),
            ("agent", "Show AGENT.md"),
            ("note", "Record an explicit note"),
            ("distill", "Distill recent candidates"),
            ("pending", "Show the pending AGENT update"),
            ("approve", "Approve the pending update"),
            ("reject", "Reject the pending update"),
            ("rollback", "Roll back AGENT.md"),
            ("files", "Show memory file paths"),
        ),
    )
)
