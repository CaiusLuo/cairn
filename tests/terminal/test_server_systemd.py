import shutil
import subprocess
from pathlib import Path

import pytest


def test_server_unit_verifies(tmp_path: Path) -> None:
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze is unavailable")
    text = Path("deploy/systemd/cairn.service.in").read_text()
    values = {
        "ROOT": str(Path.cwd()),
        "USER": "root",
        "HOME": "/root",
        "XDG_STATE_HOME": "/root/.local/state",
        "WORKSPACE": str(tmp_path / "source"),
        "STATE": str(tmp_path / "state"),
        "WORKTREES": str(tmp_path / "worktrees"),
        "PROVIDER_APPROVAL": str(tmp_path / "provider.json"),
        "PORT": "18779",
    }
    for key, value in values.items():
        text = text.replace(f"__CAIRN_{key}__", value)
    unit = tmp_path / "cairn.service"
    unit.write_text(text)
    result = subprocess.run(
        [analyze, "verify", str(unit)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
