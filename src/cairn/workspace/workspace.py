from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path

    def __post_init__(self) -> None:
        resolve = self.root.resolve()

        if not resolve.is_dir():
            raise ValueError(f"Workspace does not exist: {resolve}")

        object.__setattr__(self, "root", resolve)
