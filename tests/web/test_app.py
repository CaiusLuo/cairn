from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from cairn.development.service import TaskBusy
from cairn.interfaces.web.app import create_app
from cairn.interfaces.web.config import WebConfig
from cairn.interfaces.web.management import management_routes


def _app(tmp_path: Path) -> Starlette:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = WebConfig(
        workspace=workspace,
        sessions_dir=tmp_path / "sessions",
        tasks_dir=tmp_path / "tasks",
        web_root=None,
    )
    return create_app(config)


class _BusyControls:
    workspace = Path("/tmp/workspace")

    async def command(self, request: dict[str, Any]) -> dict[str, Any]:
        raise TaskBusy("Task is still executing")

    async def snapshot(self, section: str) -> dict[str, Any]:
        return {}

    def commands(self) -> list[dict[str, Any]]:
        return []


def test_management_returns_explicit_task_conflict_codes() -> None:
    app = Starlette(routes=management_routes(_BusyControls(), "host-token"))
    client = TestClient(app, base_url="http://127.0.0.1")
    response = client.post(
        "/api/runtime/commands",
        headers={"Origin": "http://127.0.0.1"},
        json={"command": "/reply", "request_id": "reply-1"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "task_busy"


def test_health_and_snapshot_are_read_only(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), base_url="http://127.0.0.1")
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"ok": True, "version": 1, "read_only": True}
    snapshot = client.get("/api/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["read_only"] is True
    assert client.post("/api/snapshot").status_code == 405


def test_host_and_origin_guards_reject_untrusted_browser_requests(
    tmp_path: Path,
) -> None:
    client = TestClient(_app(tmp_path), base_url="http://127.0.0.1")
    assert (
        client.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    )
    assert (
        client.get(
            "/api/health",
            headers={"origin": "http://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/health",
            headers={"origin": "http://127.0.0.1:8765"},
        ).status_code
        == 200
    )


def test_websocket_status_closes_cleanly(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), base_url="http://127.0.0.1")
    with client.websocket_connect("/api/ws", headers={"host": "127.0.0.1"}) as socket:
        message = socket.receive_json()
        assert message["type"] == "ready"
        assert message["read_only"] is True


def test_task_status_update_is_persisted_with_revision_guard(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tasks = tmp_path / "tasks"
    from cairn.development.board.models import TaskBoardStatus, TaskSource
    from cairn.development.board.store import TaskBoardStore

    store = TaskBoardStore(tasks)
    task = store.create(title="W1 move me", source=TaskSource.WEB)
    config = WebConfig(
        workspace=workspace,
        sessions_dir=tmp_path / "sessions",
        tasks_dir=tasks,
        web_root=None,
    )
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    before = client.get("/api/snapshot").json()
    response = client.patch(
        f"/api/tasks/{task.task_id}",
        json={"status": "running", "expected_revision": task.revision},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["task"]["status"] == "running"
    assert body["execution_unchanged"] is True
    assert body["sequence"] > before["sequence"]
    assert TaskBoardStore(tasks).load()[0].status is TaskBoardStatus.RUNNING

    stale = client.patch(
        f"/api/tasks/{task.task_id}",
        json={"status": "waiting", "expected_revision": task.revision},
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == "revision_conflict"
    assert TaskBoardStore(tasks).load()[0].status is TaskBoardStatus.RUNNING


def test_task_status_update_validates_status_and_content_type(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tasks = tmp_path / "tasks"
    from cairn.development.board.store import TaskBoardStore

    task = TaskBoardStore(tasks).create(title="W1 validation")
    config = WebConfig(
        workspace=workspace,
        sessions_dir=tmp_path / "sessions",
        tasks_dir=tasks,
        web_root=None,
    )
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    assert (
        client.patch(
            f"/api/tasks/{task.task_id}",
            json={"status": "not-a-status", "expected_revision": 1},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/tasks/{task.task_id}",
            content="not-json",
            headers={"content-type": "text/plain"},
        ).status_code
        == 415
    )


def test_login_cookie_gates_api_reads(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = WebConfig(
        workspace=workspace,
        sessions_dir=tmp_path / "sessions",
        tasks_dir=tmp_path / "tasks",
        web_root=None,
        auth_passphrase="correct-horse",
    )
    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    assert client.get("/api/snapshot").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert (
        client.post(
            "/api/login",
            json={"username": "cairn", "auth_passphrase": "wrong"},
        ).status_code
        == 401
    )
    login = client.post(
        "/api/login",
        json={"username": "cairn", "auth_passphrase": "correct-horse"},
    )
    assert login.status_code == 200
    assert login.json() == {"ok": True}
    assert client.get("/api/snapshot").status_code == 200
