"""Locate Vite assets in an installed wheel or a source checkout."""

from pathlib import Path


def default_web_root() -> Path:
    packaged = Path(__file__).resolve().parent / "static"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[4] / "web/dist"


def require_web_assets(root: Path) -> Path:
    required = ("index.html", "chat.html", "tasks.html", "settings.html", "login.html")
    if (
        not all((root / name).is_file() for name in required)
        or not (root / "assets").is_dir()
    ):
        raise ValueError(
            f"Web assets missing at {root}; run pnpm --dir web install --frozen-lockfile && "
            "pnpm --dir web build before starting Cairn or building its wheel"
        )
    return root
