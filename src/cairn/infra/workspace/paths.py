from pathlib import Path


def _reject_symlink_components(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part

        if current.is_symlink():
            raise ValueError("symlink targets are not supported")


def resolve_workspace_path(root: Path, raw: object) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("path must be a non-empty workspace-relative string")

    relative = Path(raw)

    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
        raise ValueError("path must stay inside the workspace")

    _reject_symlink_components(root, relative)

    path = (root / relative).resolve()

    if not path.is_relative_to(root):
        raise ValueError("path must stay inside the workspace")

    resolved_relative = path.relative_to(root)

    if ".git" in resolved_relative.parts:
        raise ValueError("path must stay outside .git")

    return path
