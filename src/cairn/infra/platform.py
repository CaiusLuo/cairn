"""Platform capability checks for descriptor-relative, symlink-safe access.

Several of Cairn's trust boundaries open a path one component at a time and
refuse a replaced symlink. That needs POSIX open flags plus ``dir_fd`` and
``follow_symlinks`` support. A host without those capabilities must not run with
the protection silently switched off; it must refuse before any filesystem
effect.

Importing this module never fails on a platform that lacks a flag: the flags are
read inside the operation that needs them, so ``cairn --help`` stays importable
everywhere while an unsafe operation refuses where it actually runs.
"""

import os

# Capability is a property of the host, fixed for the process lifetime. Record it
# once at import so a later monkeypatch of ``os.open``/``os.stat`` (a test double,
# for instance) does not make a real capability look absent, and so the check
# itself costs nothing per call. A host that lacks the capability records False
# here and every operation refuses.
_HOST_DIR_FD = os.open in os.supports_dir_fd
_HOST_NO_FOLLOW_STAT = os.stat in os.supports_follow_symlinks


class CapabilityMissing(OSError):
    """The host lacks a capability a filesystem operation depends on.

    A distinct ``OSError`` so callers keep their existing component error
    semantics (``SessionStoreError``, ``MemoryError``, …) while the cause says
    exactly which capability was missing.
    """


def flag(name: str) -> int:
    """Return a POSIX open flag, or raise if the host does not provide it.

    Call this inside the operation, not at import: a missing flag must refuse the
    operation, never break module import.
    """
    value = getattr(os, name, None)
    if not isinstance(value, int):
        raise CapabilityMissing(f"required platform capability missing: {name}")
    return value


def open_flags(*names: str) -> int:
    """Combine ``O_RDONLY`` with each named flag, refusing a missing one."""
    flags = os.O_RDONLY
    for name in names:
        flags |= flag(name)
    return flags


def require_dir_fd() -> None:
    """Refuse when ``os.open`` cannot be told to open relative to a descriptor.

    ``dir_fd`` is what keeps a walk anchored to an already-verified parent, so a
    replaced ancestor cannot redirect the next component.
    """
    if not _HOST_DIR_FD:
        raise CapabilityMissing("required platform capability missing: dir_fd")


def require_no_follow_stat() -> None:
    """Refuse when ``os.stat`` cannot inspect a name without following a link.

    Without ``follow_symlinks=False`` the identity re-check that detects a
    replaced symlink cannot be performed.
    """
    if not _HOST_NO_FOLLOW_STAT:
        raise CapabilityMissing(
            "required platform capability missing: follow_symlinks=False"
        )
