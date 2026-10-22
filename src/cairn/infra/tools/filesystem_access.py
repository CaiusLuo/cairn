"""Validation of directories an agent may access outside its workspace.

``core.permissions`` owns the grant *decision*; the filesystem *effect* of
proving that a requested path is a real, non-protected directory belongs here,
in ``infra``, beside the workspace sandbox it widens. One callable is the whole
contract and is injected into ``SessionPermissionHandler``, so ``core`` depends
on a function rather than on the filesystem.
"""

import os
from pathlib import Path

#: Absolute directories that may never be granted, even with approval. These
#: are the filesystem root, a home/system tree that would expose credentials or
#: the whole machine, or a pseudo-filesystem. A grant is scoped to a
#: project-adjacent data directory, never to one of these or anything inside it.
_BLOCKED_SYSTEM_ROOTS: frozenset[Path] = frozenset(
    {
        Path("/"),
        Path("/home"),
        Path("/root"),
        Path("/Users"),
        Path("/etc"),
        Path("/usr"),
        Path("/lib"),
        Path("/lib32"),
        Path("/lib64"),
        Path("/bin"),
        Path("/sbin"),
        Path("/boot"),
        Path("/dev"),
        Path("/proc"),
        Path("/sys"),
    }
)


def normalize_access_root(raw: object, *, protected: tuple[Path, ...] = ()) -> Path:
    """Validate one requested access root and return its resolved directory.

    The path is resolved first (symlinks followed) and then every check applies
    to the real target, so a link cannot smuggle in a blocked or protected tree.
    It must be an absolute path to an existing directory that is not a blocked
    system tree (or inside one) and does not include a ``.git`` component.
    ``protected`` adds caller-specific forbidden roots — the workspace and the
    user's home — refusing a grant that is, contains, or sits inside them.

    Raises:
        ValueError: the value cannot be granted.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("filesystem path must be a non-empty string")
    if os.sep != "/":
        # The sandbox implementation this capability targets is POSIX-only.
        raise ValueError("filesystem capability requires a POSIX path")

    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("filesystem path must be absolute")

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("filesystem path does not exist") from None

    if not resolved.is_dir():
        raise ValueError("filesystem path must be a directory")

    # Check both the path as given and its resolved target. macOS aliases the
    # system trees through /private (e.g. /etc -> /private/etc), so a check on
    # only one form would let the other alias a blocked tree.
    for blocked in (candidate, resolved):
        _reject_blocked(blocked)

    for forbidden in protected:
        if (
            resolved == forbidden
            or forbidden in resolved.parents
            or resolved in forbidden.parents
        ):
            raise ValueError("filesystem path overlaps a protected root")

    if any(part.casefold() == ".git" for part in resolved.parts):
        raise ValueError("filesystem path may not include Git metadata")

    return resolved


def _reject_blocked(path: Path) -> None:
    """Refuse a path that is a blocked system root or sits inside one.

    ``/`` is special-cased: every absolute path is a descendant of it, so only
    the root itself is refused.
    """
    for blocked in _BLOCKED_SYSTEM_ROOTS:
        if blocked == path or (blocked != Path("/") and blocked in path.parents):
            raise ValueError("filesystem path is a protected system location")
