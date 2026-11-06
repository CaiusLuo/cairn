"""Real Debian process admission, shared storage and restart acceptance."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from cairn.infra.wechat.state import WechatServeLock
from tests.support.git import initialize_repository
from tests.support.server_process import ROOT, ServerProcess


def configured(tmp_path: Path) -> ServerProcess:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    return ServerProcess(source, tmp_path)


def invoke(server: ServerProcess) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cairn.server", "--config", str(server.config)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_custom_root_web_api_and_restart_share_persistent_state(tmp_path: Path) -> None:
    server = configured(tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        with httpx.Client(base_url=server.url, headers=headers, trust_env=False) as api:
            for page in ("/chat.html", "/tasks.html", "/settings.html", "/index.html"):
                assert api.get(page).status_code == 200
            assert api.get("/api/health").status_code == 200
            assert api.get("/api/runtime/tasks").json() == []
            assert api.get("/api/runtime/bootstrap").json()["data_root"] == str(
                server.data_root
            )
            command = subprocess.run(
                [
                    str(Path(sys.executable).parent / "cairn-cli"),
                    "--config",
                    str(server.config),
                    "runtime",
                    "list",
                ],
                cwd=server.source,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert command.returncode == 0, command.stderr
            assert json.loads(command.stdout) == []
            assert api.get("/api/health").status_code == 200
        assert (server.data_root / "agent/AGENT.md").is_file()
        assert (server.data_root / "schedule/schedule.lock").is_file()
        assert (server.state / "sessions/sessions.sqlite3").is_file()
        assert not (server.state / "host-state").exists()
        assert not (server.state / "agent").exists()
        marker = server.data_root / "agent/MEMORY.md"
        marker.write_text("# Persisted personal memory\n")
        server.stop()
        server.start()
        assert marker.read_text() == "# Persisted personal memory\n"
        assert (server.state / "runtime.token").read_text().strip() == token
    finally:
        server.stop()


@pytest.mark.parametrize(
    "problem", ["port", "assets", "provider", "key", "toml", "wechat", "schedule"]
)
def test_startup_refuses_invalid_or_unavailable_dependencies(
    tmp_path: Path, problem: str
) -> None:
    server = configured(tmp_path)
    held = socket.socket()
    try:
        text = server.config.read_text()
        if problem == "port":
            held.bind(("127.0.0.1", server.port))
            held.listen()
        elif problem == "assets":
            text += 'web_root = "' + str(tmp_path / "missing-assets") + '"\n'
        elif problem == "provider":
            text = text.replace(
                "offline = true",
                'provider_approval = "' + str(tmp_path / "missing-provider.json") + '"',
            )
        elif problem == "key":
            models = server.source / ".cairn/models.toml"
            models.parent.mkdir(exist_ok=True)
            models.write_text(
                '[[providers]]\nname = "example"\nbase_url = "https://example.invalid/v1"\napi_key_env = "CAIRN_P0A_MISSING_KEY"\n[[providers.models]]\nname = "primary"\nmodel_ids = ["openai/test"]\n'
            )
            approval = tmp_path / "provider.json"
            approval.write_text(
                json.dumps(
                    {
                        "name": "example",
                        "endpoint": "https://example.invalid/v1",
                        "group": "primary",
                        "key_env": "CAIRN_P0A_MISSING_KEY",
                        "model_ids": ["openai/test"],
                    }
                )
            )
            text = text.replace(
                "offline = true", 'provider_approval = "' + str(approval) + '"'
            )
        elif problem == "toml":
            text += 'api_key = "sentinel-secret"\n'
        elif problem == "wechat":
            server.state.mkdir(parents=True)
            (server.state / "controls.json").write_text(
                json.dumps({"version": 1, "wechat_enabled": True})
            )
        elif problem == "schedule":
            root = server.data_root / "schedule"
            root.mkdir(parents=True)
            (root / "jobs.json").write_text("damaged")
        server.config.write_text(text)
        result = invoke(server)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "sentinel-secret" not in result.stdout + result.stderr
        assert "failed" in (result.stdout + result.stderr).lower()
        if problem in {"port", "assets", "provider", "key", "toml"}:
            assert not (server.state / "sessions").exists()
    finally:
        held.close()


def test_same_wechat_account_is_exclusive_across_processes_and_data_roots(
    tmp_path: Path,
) -> None:
    owner = WechatServeLock(tmp_path / "first")
    owner.acquire(description="test", account_id="fake-p0a-account")
    try:
        code = (
            "from pathlib import Path; "
            "from cairn.infra.wechat.state import WechatServeLock; "
            "lock=WechatServeLock(Path(" + repr(str(tmp_path / "second")) + ")); "
            "lock.acquire(description='second', account_id='fake-p0a-account')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "already owns this WeChat account" in result.stderr
    finally:
        owner.release()
    successor = WechatServeLock(tmp_path / "second")
    successor.acquire(description="successor", account_id="fake-p0a-account")
    successor.release()


def test_default_worktrees_under_data_root_remain_agent_writable(
    tmp_path: Path,
) -> None:
    from tests.support.server_process import wait_for

    server = configured(tmp_path)
    server.config.write_text(
        "\n".join(
            line
            for line in server.config.read_text().splitlines()
            if not line.startswith("worktrees_root")
        )
        + "\n"
    )
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        with httpx.Client(
            base_url=server.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as api:
            task = api.post(
                "/api/runtime/tasks",
                json={
                    "prompt": "Create the nested worktree artifact",
                    "source": "web",
                    "mode": "development",
                },
            ).json()
            completed = wait_for(
                lambda: (
                    value
                    if (
                        value := api.get("/api/runtime/tasks/" + task["task_id"]).json()
                    )["status"]
                    == "completed"
                    else None
                )
            )
        tree = Path(completed["worktree"])
        assert tree.is_relative_to(server.data_root / "worktrees")
        assert (tree / "runtime-result.txt").is_file()
        assert not (tree / "runtime").exists()
    finally:
        server.stop()
