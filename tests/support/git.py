import shlex
import subprocess
import sys
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


def configure_side_effect_filter(root: Path, marker: Path) -> None:
    """Install a local clean filter whose only extra effect is a test canary."""
    script = marker.with_suffix(".py")
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    )
    (root / ".gitattributes").write_text("tracked.txt filter=test-canary\n")
    git(
        root,
        "config",
        "filter.test-canary.clean",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
    )
    (root / "tracked.txt").write_text("before\n")


def configure_missing_promisor_tree(root: Path, marker: Path) -> None:
    """Remove a fixture's loose tree and detect any attempted lazy fetch."""
    script = marker.with_suffix(".py")
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "raise SystemExit(1)\n"
    )
    git(root, "config", "remote.origin.url", str(root))
    git(root, "config", "remote.origin.promisor", "true")
    git(root, "config", "remote.origin.partialclonefilter", "blob:none")
    git(
        root,
        "config",
        "remote.origin.uploadpack",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
    )
    tree = git(root, "rev-parse", "HEAD^{tree}")
    (root / ".git" / "objects" / tree[:2] / tree[2:]).unlink()
