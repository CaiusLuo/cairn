"""Real HTTP, PTY, Git and SQLite acceptance with unpaid model doubles."""

import asyncio
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from cairn.infra.runtime_client import RuntimeClient
from cairn.terminal.wechat.runtime import WechatRuntimeAdapter
from tests.support.git import initialize_repository
from tests.support.server_process import NativeClient, ServerProcess, wait_for

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux Server/PTY acceptance"
)


def test_client_exit_reconnect_web_and_crash_recovery(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    (server.gates / "before").touch()
    terminals: list[NativeClient] = []
    try:
        server.start()
        duplicate = subprocess.run(
            [
                str(Path(sys.executable).parent / "cairn"),
                "--workspace",
                str(source),
                "--state-dir",
                str(server.state),
                "--worktrees",
                str(tmp_path / "worktrees"),
                "--offline",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert duplicate.returncode != 0
        assert "Another runtime owns" in duplicate.stderr
        token_file = server.state / "runtime.token"
        token = token_file.read_text().strip()
        with httpx.Client(
            base_url=server.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            timeout=5,
        ) as api:
            a = NativeClient(source, server.url, token_file)
            terminals.append(a)
            wait_for(lambda: b"Shared Runtime" in a.output)
            a.send("/develop Create the acceptance artifact")
            wait_for(lambda: (server.gates / "before.entered").exists())
            task = api.get("/api/runtime/tasks").json()[0]
            task_id = task["task_id"]
            assert task["status"] == "running"
            a.send("/exit")
            assert a.process.wait(timeout=10) == 0, bytes(a.output).decode(
                errors="replace"
            )[-2000:]
            assert (
                api.get(f"/api/runtime/tasks/{task_id}").json()["status"] == "running"
            )
            # A second real CLI attaches to the same still-running task.
            b = NativeClient(source, server.url, token_file, task_id)
            terminals.append(b)
            wait_for(lambda: task_id.encode() in b.output)
            b.process.send_signal(signal.SIGHUP)
            assert b.process.wait(timeout=10) == -signal.SIGHUP
            assert (
                api.get(f"/api/runtime/tasks/{task_id}").json()["status"] == "running"
            )
            bridge = WechatRuntimeAdapter(RuntimeClient(server.url, token))
            assert task_id in asyncio.run(bridge.reply("/status " + task_id))
            # Browser cookie authentication observes the same live task.
            with httpx.Client(base_url=server.url, trust_env=False, timeout=5) as web:
                assert (
                    web.post(
                        "/api/login",
                        json={"username": "cairn", "auth_passphrase": token},
                    ).status_code
                    == 200
                )
                wait_for(lambda: task_id in web.get("/api/snapshot").text)
                assert (
                    web.get(f"/api/runtime/tasks/{task_id}").json()["status"]
                    == "running"
                )
            (server.gates / "before").unlink()
            done = wait_for(
                lambda: (
                    value
                    if (value := api.get(f"/api/runtime/tasks/{task_id}").json())[
                        "status"
                    ]
                    == "completed"
                    else None
                )
            )
            artifact = Path(done["worktree"]) / "runtime-result.txt"
            inode = artifact.stat().st_ino
            assert artifact.read_text() == "Cairn shared runtime task completed\n"
            server.stop()
            server.start()
            restored = api.get(f"/api/runtime/tasks/{task_id}").json()
            assert (
                restored["session_id"],
                restored["worktree"],
                restored["status"],
            ) == (done["session_id"], done["worktree"], "completed")
            assert artifact.stat().st_ino == inode
            # Kill after a real write but before answer settlement: never replay it.
            (server.gates / "after").touch()
            second = api.post(
                "/api/runtime/tasks",
                json={
                    "prompt": "Create another artifact",
                    "source": "web",
                    "mode": "development",
                },
            ).json()
            wait_for(lambda: (server.gates / "after.entered").exists())
            second = api.get(f"/api/runtime/tasks/{second['task_id']}").json()
            effect = Path(second["worktree"]) / "runtime-result.txt"
            effect_inode = effect.stat().st_ino
            server.stop(crash=True)
            server.start()
            path = f"/api/runtime/tasks/{second['task_id']}"
            recovered = api.get(path).json()
            assert recovered["status"] == "recovery_required", recovered
            assert recovered["worktree"] == second["worktree"]
            assert api.post(path + "/continue").status_code == 409
            assert effect.stat().st_ino == effect_inode
            report = api.get(path + "/recovery").json()
            review = {
                key: report[key] for key in ("expected_sequence", "revision", "action")
            }
            review.update(
                reviewed_effects=True,
                note="Host inspected the existing artifact; do not replay the write",
            )
            response = api.post(path + "/reconcile", json=review)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "paused"
            assert effect.stat().st_ino == effect_inode
            assert len(api.get("/api/runtime/tasks").json()) == 2
    finally:
        for terminal in terminals:
            terminal.close()
        server.stop()


def test_server_reports_invalid_configuration_without_starting_an_agent(
    tmp_path: Path,
) -> None:
    command = Path(sys.executable).parent / "cairn"
    result = subprocess.run(
        [
            str(command),
            "--workspace",
            str(tmp_path),
            "--provider-approval",
            str(tmp_path.parent / "missing-approval.json"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "Server startup failed" in result.stderr
    assert "Traceback" not in result.stderr
