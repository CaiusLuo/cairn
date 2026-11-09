"""Credential-bearing paths a file tool must not read without approval.

Cairn can search the whole machine, but a credential file is not ordinary data.
This module owns the one answer to "is this path a credential?" so every reader
that can leave the Workspace asks the same question instead of each keeping a
partial list.

Only credentials are listed. An ordinary hidden file such as ``.gitignore`` or
``.prettierrc`` is searchable; a private key is not. ``.git`` metadata is a
separate concern owned by ``infra.workspace.paths``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

#: Directory names whose contents are credentials, at any depth.
CREDENTIAL_DIRECTORIES = frozenset(
    {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker"}
)

#: Exact file names that hold credentials.
CREDENTIAL_FILENAMES = frozenset(
    {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", ".netrc"}
)

#: Suffixes that mark a file as key material.
CREDENTIAL_SUFFIXES = (
    ".token",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".keystore",
    ".env",
)


_HOST_PRIVATE_ROOTS: ContextVar[tuple[Path, ...]] = ContextVar(
    "cairn_host_private_roots", default=()
)


@contextmanager
def protect_host_paths(roots: tuple[Path, ...]) -> Iterator[None]:
    """Narrow file-tool visibility in this Agent task, never process-globally."""
    token = _HOST_PRIVATE_ROOTS.set(tuple(root.resolve() for root in roots))
    try:
        yield
    finally:
        _HOST_PRIVATE_ROOTS.reset(token)


def is_credential_path(path: Path) -> bool:
    """Whether ``path`` names credential material.

    The decision is name-based and does not touch the filesystem, so it is safe
    to apply to a candidate before any stat or read. Both the file name and every
    directory component are examined: a key inside ``~/.ssh`` is refused even
    when its own name looks ordinary.
    """
    if any(path.is_relative_to(root) for root in _HOST_PRIVATE_ROOTS.get()):
        return True
    if path.name.casefold() in CREDENTIAL_FILENAMES:
        return True
    if path.name.casefold() == ".env" or path.name.startswith(".env."):
        return True
    if any(path.name.endswith(suffix) for suffix in CREDENTIAL_SUFFIXES):
        return True
    return any(part.casefold() in CREDENTIAL_DIRECTORIES for part in path.parts)
