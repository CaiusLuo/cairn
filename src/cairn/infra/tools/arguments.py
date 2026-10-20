"""Argument preflight shared by every model-facing tool.

This module owns the argument checks that guard the tool trust boundary:

- an argument key the tool did not declare,
- a NUL inside a path,
- a path that leaves the workspace (``..``, absolute, ``.git`` or a symlink
  component).

Tools call these entry points instead of re-deriving the checks, so workspace
containment and NUL rejection have exactly one owner. Every rejection is
``InvalidArguments`` and fails closed. Tool-specific policy (line ranges, patch
shape, content size, pattern syntax) stays with the tool that declares it.

Ordering is contractual: a tool's ``validate`` calls this preflight before it
touches the filesystem, and runtime performs ``validate`` before permission
checks.
"""

from collections.abc import Collection
from pathlib import Path
from typing import Any

from cairn.infra.tools.base import InvalidArguments
from cairn.infra.workspace.workspace import Workspace


def function_schema(
    name: str, description: str, parameters: dict[str, Any]
) -> dict[str, Any]:
    """Wrap one tool's parameter schema in the model-visible function envelope."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def reject_unknown_keys(
    arguments: dict[str, Any],
    accepted: Collection[str],
    *,
    tool_label: str,
) -> None:
    """Reject any argument key the tool did not declare.

    ``tool_label`` only names the tool in the model-facing error.
    """
    if set(arguments) - set(accepted):
        raise InvalidArguments(f"unexpected {tool_label} argument")


def workspace_path(workspace: Workspace, arguments: dict[str, Any]) -> Path:
    """Validate a required ``path`` argument and return its workspace target.

    A NUL, a non-string, an empty value and any path outside the workspace raise
    ``InvalidArguments`` before the caller touches the filesystem. The returned
    path is resolved, so symlink components have already been rejected.
    """
    raw_path = arguments.get("path")
    if isinstance(raw_path, str) and "\0" in raw_path:
        raise InvalidArguments("path must not contain NUL")
    try:
        return workspace.resolve_path(raw_path)
    except ValueError as exc:
        raise InvalidArguments(str(exc)) from exc
