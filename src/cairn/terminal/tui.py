"""Launch the separately installed experimental Pi frontend."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer


def launch(ui_dir: Path | None = None) -> None:
    node = shutil.which("node")
    root = ui_dir or Path(__file__).resolve().parents[3] / "ui" / "pi"
    entry = root.resolve() / "dist" / "main.js"
    if not node or not entry.is_file():
        typer.echo(
            "Build ui/pi with pnpm install && pnpm build; use --ui-dir for an installed wheel.",
            err=True,
        )
        raise typer.Exit(1)
    version = (
        subprocess.run([node, "--version"], capture_output=True, text=True, check=True)
        .stdout.strip()
        .lstrip("v")
    )
    if tuple(int(n) for n in version.split(".")[:3]) < (22, 19, 0):
        typer.echo("Pi TUI requires Node.js >=22.19.0.", err=True)
        raise typer.Exit(1)
    os.execve(
        node, [node, str(entry)], {**os.environ, "CAIRN_TUI_PYTHON": sys.executable}
    )
