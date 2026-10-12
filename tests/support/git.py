import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def initialize_repository(
    root: Path,
    *,
    filename: str = "tracked.txt",
    contents: str = "before\n",
) -> Path:
    """Create a small committed Git repository and return its tracked file."""
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Cairn Tests")
    git(root, "config", "user.email", "cairn-tests@example.invalid")
    tracked = root / filename
    tracked.write_text(contents, encoding="utf-8")
    git(root, "add", "--", tracked.name)
    git(root, "commit", "-m", "initial")
    return tracked
