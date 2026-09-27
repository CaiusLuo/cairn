from dataclasses import dataclass
from pathlib import Path

from cairn.workspace.paths import resolve_workspace_path


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path

    def __post_init__(self) -> None:
        resolve = self.root.resolve()

        if not resolve.is_dir():
            raise ValueError(f"Workspace does not exist: {resolve}")

        object.__setattr__(self, "root", resolve)

    def resolve_path(self, raw: object) -> Path:
        return resolve_workspace_path(self.root, raw)
