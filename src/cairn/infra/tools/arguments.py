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

from cairn.core.permissions import AccessKind, CapabilityRequest, PermissionCapability
from cairn.infra.tools.base import InvalidArguments, PermissionRefused
from cairn.infra.workspace.paths import is_within, resolve_tool_path
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


def access_request(
    workspace: Workspace, arguments: dict[str, Any], *, access: AccessKind
) -> CapabilityRequest | None:
    """The capability a file tool needs for its ``path`` argument, if any.

    Inside the Workspace nothing is requested. Outside it, reading needs no
    grant and writing asks for ``FILESYSTEM_ACCESS`` on the target's parent
    directory, so one approval covers that subtree for the session.
    """
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if resolved.is_relative_to(workspace.root):
        return None
    if access is AccessKind.READ:
        return None
    return CapabilityRequest(
        capability=PermissionCapability.FILESYSTEM_ACCESS,
        paths=(str(resolved),),
        justification=f"Write to {resolved} outside the workspace.",
    )


def tool_path(
    workspace: Workspace,
    arguments: dict[str, Any],
    *,
    access: AccessKind,
    granted_roots: frozenset[Path] = frozenset(),
    enforce_grant: bool = True,
) -> Path:
    """Resolve a file-tool ``path``, enforcing the outside-workspace rule.

    A path inside keeps the strict workspace rule. Outside it, reading is
    allowed and writing must fall inside an approved grant.

    ``enforce_grant`` is False for preflight, which runs before any approval: it
    checks that the path is a well-formed target, and the grant is enforced when
    the tool actually executes.
    """
    raw_path = arguments.get("path")
    if isinstance(raw_path, str) and "\0" in raw_path:
        raise InvalidArguments("path must not contain NUL")
    try:
        resolved, outside = resolve_tool_path(
            workspace.root, raw_path, writable=access is AccessKind.WRITE
        )
    except ValueError as exc:
        raise InvalidArguments(str(exc)) from exc

    if not enforce_grant or not outside or access is not AccessKind.WRITE:
        return resolved

    # Compare resolved paths: a granted root and the target must be judged in the
    # same canonical form, or a symlinked parent (e.g. /tmp on macOS) never matches.
    approved = frozenset(root.resolve() for root in granted_roots)
    if not is_within(resolved.parent.resolve(), approved):
        raise PermissionRefused("write outside the workspace requires approval")
    return resolved
