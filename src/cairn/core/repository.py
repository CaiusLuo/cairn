from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_PATHS = 50


@dataclass(frozen=True, slots=True)
class RepositoryEvidence:
    """Bounded post-run facts, not a correctness verdict.

    ``is_git_repository=None`` means inspection could not determine repository
    status. A genuine non-Git workspace has False and no inspection error.
    Paths use Workspace coordinates; HEAD may be absent on an unborn branch.
    """

    workspace_root: Path
    is_git_repository: bool | None = None
    repository_root: Path | None = None
    branch: str | None = None
    head_revision: str | None = None
    dirty: bool | None = None
    changed_files: tuple[str, ...] = ()
    untracked_files: tuple[str, ...] = ()
    truncated: bool = False
    path_limit: int = DEFAULT_MAX_PATHS
    inspection_error: str | None = None
