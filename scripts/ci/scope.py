"""Skip executable checks only for a proven documentation-only Git diff."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import PurePosixPath


def documentation_only(paths: Sequence[str]) -> bool:
    # Keep this allowlist narrow: Markdown skills, prompts and test fixtures can
    # be executable inputs. Unknown paths and an empty diff require full CI.
    return bool(paths) and all(
        path in {"README.md", "tests/README.md", "scripts/README.md"}
        or (path.startswith("docs/") and PurePosixPath(path).suffix == ".md")
        for path in paths
    )


def changed_paths(base: str, head: str, *, pull_request: bool) -> list[str]:
    if not base or not head or set(base) == {"0"}:
        return []
    if pull_request:
        base = subprocess.check_output(
            ["git", "merge-base", base, head], text=True
        ).strip()
    # A rename must expose both the removed and added path. Otherwise moving
    # code into docs could be misclassified as documentation-only.
    output = subprocess.check_output(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"]
    )
    return [os.fsdecode(path) for path in output.split(b"\0") if path]


def main() -> None:
    paths: list[str] = []
    event = os.environ.get("CI_EVENT", "")
    if event in {"push", "pull_request"}:
        try:
            paths = changed_paths(
                os.environ.get("CI_BASE", ""),
                os.environ.get("CI_HEAD", ""),
                pull_request=event == "pull_request",
            )
        except subprocess.CalledProcessError:
            print("Cannot establish changed paths; running full CI.")
    docs_only = documentation_only(paths)
    print(f"CI scope: {'documentation only' if docs_only else 'full acceptance'}")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"docs_only={str(docs_only).lower()}\n")


if __name__ == "__main__":
    main()
