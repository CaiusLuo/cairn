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
        "CONFIG": str(tmp_path / "server.toml"),
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

    def wrapper(
        action: str, config: Path, *, succeeds: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["bash", "scripts/start.sh", action],
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("CAIRN_")
                },
                "CAIRN_CONFIG": str(config),
                "CAIRN_SERVICE": name,
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        if succeeds:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0
        return result

    name = "cairn-p0-test-" + uuid4().hex + ".service"
    unit = Path("/run/systemd/system") / name
    with tempfile.TemporaryDirectory(prefix="cairn-p0-", dir="/run") as directory:
        root = Path(directory)
        source = root / "source"
        source.mkdir()
        initialize_repository(source)
        port = free_port()
        text = Path("deploy/systemd/cairn.service.in").read_text()
        import json

        config = root / "server.toml"
        config.write_text(
            "[server]\nworkspace = "
            + json.dumps(str(source))
            + "\ndata_root = "
            + json.dumps(str(root / "state"))
            + "\nworktrees_root = "
            + json.dumps(str(root / "worktrees"))
            + "\noffline = true\nport = "
            + str(port)
            + "\n[budget]\nmax_steps = 1\n"
        )
        text = text.replace("EnvironmentFile=-/etc/cairn/cairn.env\n", "")
        values = {
            "ROOT": str(Path.cwd()),
            "USER": pwd.getpwuid(os.geteuid()).pw_name,
            "CONFIG": str(config),
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
            wrapper("start", config)
            wait_for(ready)
            token = (root / "state/runtime/runtime.token").read_text().strip()
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
                wrapper("start", config)
                assert control("show", name, "-p", "MainPID", "--value") == pid
                original = config.read_text()
                config.write_text(
                    original.replace(str(root / "state"), str(root / "changed-state"))
                )
                refused = wrapper("restart", config, succeeds=False)
                assert "Cannot identify running Server" in refused.stderr
                assert control("show", name, "-p", "MainPID", "--value") == pid
                config.write_text(original)
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
                wrapper("restart", config)
                wait_for(ready)
                assert api.get(path).json()["status"] == "completed"
                assert artifact.stat().st_ino == inode
            wrapper("stop", config)
            assert control("show", name, "-p", "ActiveState", "--value") == "inactive"
        finally:
            subprocess.run(["systemctl", "stop", name], capture_output=True, timeout=30)
            subprocess.run(
                ["systemctl", "reset-failed", name], capture_output=True, timeout=10
            )
            unit.unlink(missing_ok=True)
            control("daemon-reload")
