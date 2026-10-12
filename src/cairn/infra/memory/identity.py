"""Git common-directory identity; independent clones intentionally remain separate."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cairn.core.memory.models import MemoryError
from cairn.infra.git.context import RepoContextProvider
from cairn.infra.git.environment import build_git_env
from cairn.infra.workspace.workspace import Workspace


async def git_read(workspace: Workspace, *args: str) -> bytes:
    """Reuse bounded subprocess/cancellation handling. Never invoke filters or hooks."""
    env = build_git_env(os.environ)
    env["GIT_NO_LAZY_FETCH"] = "1"
    provider = RepoContextProvider(workspace, secret_env_keys=frozenset())
    try:
        result = await provider.run_git(
            "--no-replace-objects", "--literal-pathspecs", *args, env=env
        )
        if result.returncode or result.stdout_truncated or result.stderr_truncated:
            raise MemoryError("Memory Git evidence unavailable")
        return result.stdout
    except (OSError, ValueError, TimeoutError):
        raise MemoryError("Memory Git evidence unavailable") from None


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    repository_root: Path
    workspace_prefix: str

    @classmethod
    async def inspect(cls, workspace: Workspace) -> "ProjectIdentity":
        try:
            common = Path(
                os.fsdecode(
                    (
                        await git_read(
                            workspace,
                            "rev-parse",
                            "--path-format=absolute",
                            "--git-common-dir",
                        )
                    ).strip()
                )
            ).resolve(strict=True)
            root = Path(
                os.fsdecode(
                    (await git_read(workspace, "rev-parse", "--show-toplevel")).strip()
                )
            ).resolve(strict=True)
            prefix = workspace.root.relative_to(root).as_posix()
            return cls(
                hashlib.sha256(os.fsencode(common)).hexdigest(),
                root,
                "" if prefix == "." else prefix + "/",
            )
        except (OSError, ValueError):
            raise MemoryError("Memory project identity unavailable") from None
