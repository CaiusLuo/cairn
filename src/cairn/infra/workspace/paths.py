"""The single owner of "is this path inside the Workspace".

Callers pass a workspace-relative value and get either a contained result or a
rejection. ``resolve_workspace_path`` is the path rule; ``validate_workspace_pattern``
and ``contained_candidate`` apply that same rule to a glob pattern and to one
glob candidate, so a search cannot answer with a path that a direct tool call
would refuse.
"""

import os
from pathlib import Path


def _reject_symlink_components(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part

        if current.is_symlink():
            raise ValueError("symlink targets are not supported")


def _is_wildcard(part: str) -> bool:
    return any(character in part for character in "*?[")


def _reject_symlink_pattern_prefix(root: Path, relative: Path) -> None:
    """Check the literal components that precede a pattern's first wildcard.

    A wildcard stands for many entries rather than naming one link, so it ends
    the part of the pattern that can be checked as a path. Everything a wildcard
    still reaches is checked per candidate by ``contained_candidate``.
    """
    current = root
    for part in relative.parts:
        if _is_wildcard(part):
            return
        current = current / part

        if current.is_symlink():
            raise ValueError("symlink targets are not supported")


def _is_git_part(part: str) -> bool:
    """Whether this path component names Git metadata, whatever its case.

    A case-insensitive filesystem resolves ``.GIT/config`` to the same file as
    ``.git/config``, so an exact-case comparison would let a tool edit the very
    metadata the sandbox is required to protect.
    """
    return part.casefold() == ".git"


def _has_git_part(parts: "tuple[str, ...] | list[str]") -> bool:
    return any(_is_git_part(part) for part in parts)


def _relative_parts(raw: object) -> Path:
    """Split a workspace-relative argument into parts, rejecting an escape.

    Absolute values and ``..`` or ``.git`` parts can leave the Workspace before
    any filesystem check runs, so they are rejected here for both plain paths
    and glob patterns.
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError("path must be a non-empty workspace-relative string")

    relative = Path(raw)

    if (
        relative.is_absolute()
        or ".." in relative.parts
        or _has_git_part(relative.parts)
    ):
        raise ValueError("path must stay inside the workspace")

    return relative


def resolve_workspace_path(root: Path, raw: object) -> Path:
    relative = _relative_parts(raw)

    _reject_symlink_components(root, relative)

    path = (root / relative).resolve()

    if not path.is_relative_to(root):
        raise ValueError("path must stay inside the workspace")

    resolved_relative = path.relative_to(root)

    if _has_git_part(resolved_relative.parts):
        raise ValueError("path must stay outside .git")

    return path


def validate_workspace_pattern(root: Path, raw: object) -> None:
    """Reject a glob pattern whose literal parts could leave the Workspace.

    A pattern keeps its wildcards, so it is not itself a resolvable target. It
    still obeys the rest of ``resolve_workspace_path``'s rule: the same
    absolute/``..``/``.git`` rejection, and the same symlink-component rejection
    for the literal parts that precede the first wildcard. Whatever a pattern
    can still reach is re-checked per candidate by ``contained_candidate``.
    """
    relative = _relative_parts(raw)

    _reject_symlink_pattern_prefix(root, relative)


def contained_candidate(
    root: Path,
    candidate: Path,
    *,
    directory_cache: dict[Path, Path | None] | None = None,
) -> Path | None:
    """Resolve one glob candidate and keep it only while it stays in the Workspace.

    This applies ``resolve_workspace_path``'s containment rule to a path that
    glob already produced: under ``root``, no symlink component, resolved under
    ``root``, and outside ``.git``. Unlike the path rule it answers ``None``
    instead of raising, because one uncontained candidate is dropped from a
    search rather than turned into a failed search.

    Returns the contained path, or ``None`` when the candidate is not provably
    inside the Workspace. Failing closed matters here: this is the layer that
    stops ``glob`` from naming files outside the sandbox.

    ``directory_cache`` lets a search reuse the containment answer for a
    directory and its ancestors: a candidate's own directory is checked once,
    then only the final component is examined, so a wide scan is not made
    quadratic in path depth. It stores resolved directories and ``None`` for a
    rejected one, and must not be shared across Workspaces.
    """
    # Cheap lexical pre-filter. It only ever rejects: a candidate that passes
    # still has its directory walked and resolved below, so this cannot admit
    # an escaping path.
    root_prefix = f"{root}{os.sep}"
    if not str(candidate).startswith(root_prefix):
        return None

    if _is_git_part(candidate.name):
        return None

    resolved_parent = _contained_directory(root, candidate.parent, directory_cache)
    if resolved_parent is None:
        return None

    # The final component is checked here so a symlinked file is dropped even
    # though its directory is contained.
    if candidate.is_symlink():
        return None

    resolved = resolved_parent / candidate.name

    # A resolved parent is already contained, so this only guards against a name
    # that resolves elsewhere on a case-insensitive or exotic filesystem.
    if not resolved.is_relative_to(root):
        return None

    return resolved


def _contained_directory(
    root: Path,
    directory: Path,
    cache: dict[Path, Path | None] | None,
) -> Path | None:
    """The contained form of ``directory``, or ``None`` when it is not provable.

    This walks the ancestors once per directory and memoises the answer, so the
    symlink and containment checks are applied to each directory component
    exactly once per search. The result is built from validated components
    rather than by resolving again: a symlink anywhere on the path is rejected
    above, so the lexical path and the resolved path agree.
    """
    if cache is not None:
        cached = cache.get(directory)
        if cached is not None or directory in cache:
            return cached

    try:
        relative = directory.relative_to(root)
    except ValueError:
        result: Path | None = None
    else:
        if not relative.parts:
            result = root
        elif _has_git_part(relative.parts) or directory.is_symlink():
            # A link or a .git component anywhere on the path invalidates every
            # candidate beneath it, so one answer covers the whole subtree.
            result = None
        else:
            parent = _contained_directory(root, directory.parent, cache)
            result = None if parent is None else parent / directory.name

    if cache is not None:
        cache[directory] = result
    return result
