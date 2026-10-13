"""Trusted local Workspace observations for synchronous durable admission."""

import asyncio
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cairn.infra.git.context import RepoContextProvider
from cairn.infra.git.environment import build_git_env
from cairn.infra.sessions.models import (
    GitBinding,
    PathIdentity,
    SessionStoreError,
    WorkspaceBinding,
)
from cairn.infra.workspace.workspace import Workspace


def identify(path: Path) -> PathIdentity:
    path = path.resolve(strict=True)
    info = path.stat()
    if not path.is_dir():
        raise SessionStoreError("workspace_unavailable")
    return PathIdentity(path=str(path), device=info.st_dev, inode=info.st_ino)


def _pointer(path: Path) -> tuple[int, int, str]:
    if path.is_symlink() or not path.is_file():
        raise SessionStoreError("git_registration_unavailable")
    before = path.stat()
    with path.open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096 or path.stat() != before:
        raise SessionStoreError("git_registration_unavailable")
    return before.st_dev, before.st_ino, raw.decode("utf-8").strip()


async def _observe_workspace(
    workspace: Workspace, secret_env_keys: frozenset[str]
) -> WorkspaceBinding:
    root_identity = identify(workspace.root)
    provider = RepoContextProvider(workspace, secret_env_keys=secret_env_keys)
    env = build_git_env(os.environ, secret_env_keys)
    env.update(LC_ALL="C", GIT_NO_LAZY_FETCH="1")

    async def command(*args: str, optional: bool = False) -> tuple[int, str]:
        result = await provider.run_git("--no-replace-objects", *args, env=env)
        if result.stdout_truncated or result.stderr_truncated:
            raise SessionStoreError("git_observation_unavailable")
        if result.returncode and not optional:
            raise SessionStoreError("git_observation_unavailable")
        return result.returncode, result.stdout.decode("utf-8").strip()

    discovery = await provider.run_git("rev-parse", "--show-toplevel", env=env)
    if discovery.stdout_truncated or discovery.stderr_truncated:
        raise SessionStoreError("git_observation_unavailable")
    if discovery.returncode:
        # Discovery failure in a marked checkout is never a non-Git fallback.
        if any(
            (p / ".git").exists() or (p / ".git").is_symlink()
            for p in (workspace.root, *workspace.root.parents)
        ):
            raise SessionStoreError("git_observation_unavailable")
        if discovery.returncode != 128 or not discovery.stderr.startswith(
            b"fatal: not a git repository"
        ):
            raise SessionStoreError("git_observation_unavailable")
        return WorkspaceBinding(root=root_identity, git=None)
    root = Path(discovery.stdout.decode("utf-8").strip()).resolve(strict=True)
    _, common_text = await command(
        "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    _, admin_text = await command("rev-parse", "--path-format=absolute", "--git-dir")
    common, admin = Path(common_text), Path(admin_text)
    _, registrations = await command("worktree", "list", "--porcelain", "-z")
    if f"worktree {root}\0" not in registrations + "\0":
        raise SessionStoreError("git_registration_unavailable")
    pointers: list[tuple[int, int, str]] = []
    marker = root / ".git"
    if marker.is_symlink():
        raise SessionStoreError("git_registration_unavailable")
    if marker.is_file():
        pointer = _pointer(marker)
        if not pointer[2].startswith("gitdir: ") or (root / pointer[2][8:]).resolve(
            strict=True
        ) != admin.resolve(strict=True):
            raise SessionStoreError("git_registration_unavailable")
        backlink = _pointer(admin / "gitdir")
        if Path(backlink[2]).resolve(strict=True) != marker.resolve(strict=True):
            raise SessionStoreError("git_registration_unavailable")
        pointers.extend((pointer, backlink))
    elif marker.resolve(strict=True) != admin.resolve(strict=True):
        raise SessionStoreError("git_registration_unavailable")
    _, branch = await command("branch", "--show-current")
    head_code, head = await command(
        "rev-parse", "--verify", "--quiet", "HEAD^{commit}", optional=True
    )
    if head_code:
        if not branch or head_code != 1:
            raise SessionStoreError("git_observation_unavailable")
        ref_code, _ = await command(
            "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", optional=True
        )
        if ref_code != 1:
            raise SessionStoreError("git_observation_unavailable")
    prefix = workspace.root.relative_to(root).as_posix()
    observed = WorkspaceBinding(
        root=root_identity,
        git=GitBinding(
            repository=identify(root),
            common=identify(common),
            admin=identify(admin),
            workspace_prefix="" if prefix == "." else prefix + "/",
            registration_digest=hashlib.sha256(
                json.dumps([str(root), pointers]).encode()
            ).hexdigest(),
            head=None if head_code else head,
            branch=branch or None,
        ),
    )
    if identify(workspace.root) != root_identity:
        raise SessionStoreError("workspace_unavailable")
    return observed


def observe_workspace(
    workspace: Workspace, secret_env_keys: frozenset[str]
) -> WorkspaceBinding:
    """Bounded isolated Git I/O; no hooks, credentials or network discovery.

    DurableSession's host callback is synchronous. Run the existing bounded
    async Git adapter on a private thread/loop, and join it before returning or
    propagating interruption. No subprocess or observer outlives this call.
    """
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run, _observe_workspace(workspace, secret_env_keys)
            ).result()
    except Exception:
        raise SessionStoreError("workspace_observation_unavailable") from None
