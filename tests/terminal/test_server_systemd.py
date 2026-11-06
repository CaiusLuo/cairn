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


def test_systemd_restarts_server_and_retains_paused_work() -> None:
    import os
    import pwd
    import tempfile
    from uuid import uuid4

    import httpx

    from tests.support.git import initialize_repository
    from tests.support.server_process import free_port, wait_for

    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        pytest.skip("requires an isolated root-capable Debian systemd host")
    probe = subprocess.run(
        ["systemctl", "show", "-p", "Version", "--value"],
        capture_output=True,
        timeout=10,
    )
    if probe.returncode:
        pytest.skip("systemd manager is unavailable")

    def control(*args: str) -> str:
        return subprocess.run(
            ["systemctl", *args], capture_output=True, text=True, check=True, timeout=20
        ).stdout.strip()

    name = "cairn-p0-test-" + uuid4().hex + ".service"
    unit = Path("/run/systemd/system") / name
    with tempfile.TemporaryDirectory(prefix="cairn-p0-", dir="/run") as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        initialize_repository(source)
        port = free_port()
        text = Path("deploy/systemd/cairn.service.in").read_text()
        # Only the provider is replaced by the unpaid model; lifecycle is the real unit.
        text = text.replace(
            "--provider-approval __CAIRN_PROVIDER_APPROVAL__", "--offline --max-steps 1"
        )
        text = text.replace("EnvironmentFile=-/etc/cairn/cairn.env\n", "")
        values = {
            "ROOT": str(Path.cwd()),
            "USER": pwd.getpwuid(os.geteuid()).pw_name,
            "HOME": str(root),
            "XDG_STATE_HOME": str(root / "xdg"),
            "WORKSPACE": str(source),
            "STATE": str(root / "state"),
            "WORKTREES": str(root / "worktrees"),
            "PORT": str(port),
        }
        for key, value in values.items():
            text = text.replace(f"__CAIRN_{key}__", value)
        unit.write_text(text)
        url = f"http://127.0.0.1:{port}"

        def ready() -> bool:
            try:
                return (
                    httpx.get(
                        url + "/api/health", timeout=0.5, trust_env=False
                    ).status_code
                    == 200
                )
            except httpx.TransportError:
                return False

        try:
            control("daemon-reload")
            control("start", name)
            wait_for(ready)
            token = (root / "state/runtime.token").read_text().strip()
            with httpx.Client(
                base_url=url,
                headers={"Authorization": "Bearer " + token},
                trust_env=False,
                timeout=5,
            ) as api:
                response = api.post(
                    "/api/runtime/tasks",
                    json={
                        "prompt": "Create an artifact",
                        "source": "cli",
                        "mode": "development",
                    },
                )
                assert response.status_code == 200, response.text
                ident = response.json()["task_id"]
                path = "/api/runtime/tasks/" + ident
                paused = wait_for(
                    lambda: (
                        v if (v := api.get(path).json())["status"] == "paused" else None
                    )
                )
                artifact = Path(paused["worktree"]) / "runtime-result.txt"
                inode = artifact.stat().st_ino
                pid = control("show", name, "-p", "MainPID", "--value")
                control("kill", "--kill-whom=main", "--signal=KILL", name)
                wait_for(
                    lambda: (
                        control("show", name, "-p", "MainPID", "--value")
                        not in ("0", pid)
                    )
                )
                wait_for(ready)
                assert int(control("show", name, "-p", "NRestarts", "--value")) >= 1
                assert api.get(path).json()["status"] == "paused"
                continued = api.post(path + "/continue")
                assert continued.status_code == 200, continued.text
                done = wait_for(
                    lambda: (
                        v
                        if (v := api.get(path).json())["status"] == "completed"
                        else None
                    )
                )
                assert (done["session_id"], done["worktree"]) == (
                    paused["session_id"],
                    paused["worktree"],
                )
                assert artifact.stat().st_ino == inode
                control("restart", name)
                wait_for(ready)
                assert api.get(path).json()["status"] == "completed"
                assert artifact.stat().st_ino == inode
            control("stop", name)
            assert control("show", name, "-p", "ActiveState", "--value") == "inactive"
        finally:
            subprocess.run(["systemctl", "stop", name], capture_output=True, timeout=30)
            subprocess.run(
                ["systemctl", "reset-failed", name], capture_output=True, timeout=10
            )
            unit.unlink(missing_ok=True)
            control("daemon-reload")
