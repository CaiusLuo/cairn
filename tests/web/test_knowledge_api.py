"""Authenticated knowledge routes keep clips bounded and low-trust."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from cairn.interfaces.web.app import create_app
from cairn.interfaces.web.config import WebConfig

PASSPHRASE = "knowledge-passphrase"
ORIGIN = "http://127.0.0.1"


def _configured(tmp_path: Path, token: str | None = "host-token") -> Starlette:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = WebConfig(
        workspace=workspace,
        sessions_dir=tmp_path / "sessions",
        tasks_dir=tmp_path / "tasks",
        knowledge_dir=tmp_path / "knowledge",
        web_root=None,
        auth_passphrase=PASSPHRASE,
    )
    return create_app(config, runtime_token=token)


def _client(tmp_path: Path, token: str | None = "host-token") -> TestClient:
    client = TestClient(_configured(tmp_path, token), base_url=ORIGIN)
    login = client.post(
        "/api/login", json={"username": "cairn", "auth_passphrase": PASSPHRASE}
    )
    assert login.status_code == 200
    return client


def _create(client: TestClient, content: str = "original", **values: object) -> Any:
    payload = {"content": content, "source": "web", "request_id": "clip-1", **values}
    return client.post("/api/knowledge/clips", json=payload, headers={"Origin": ORIGIN})


def test_knowledge_requires_authentication_and_same_origin_mutations(
    tmp_path: Path,
) -> None:
    anonymous = TestClient(_configured(tmp_path), base_url=ORIGIN)
    assert anonymous.get("/api/knowledge/clips").status_code == 401
    assert (
        anonymous.post(
            "/api/knowledge/clips",
            json={"content": "x"},
            headers={"Origin": ORIGIN},
        ).status_code
        == 401
    )

    client = _client(tmp_path)
    assert client.get("/api/knowledge/clips").status_code == 200
    assert (
        client.post("/api/knowledge/clips", json={"content": "no origin"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/knowledge/clips",
            json={"content": "wrong origin"},
            headers={"Origin": "http://127.0.0.1:9999"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/knowledge/clips", headers={"Origin": "http://evil.example"}
        ).status_code
        == 403
    )

    bearer = TestClient(_configured(tmp_path), base_url=ORIGIN)
    assert (
        bearer.post(
            "/api/knowledge/clips",
            json={"content": "from the host", "request_id": "host-cli"},
            headers={"Authorization": "Bearer host-token"},
        ).status_code
        == 200
    )


def test_create_read_update_search_and_archive(tmp_path: Path) -> None:
    client = _client(tmp_path)
    created = _create(
        client,
        "  原文保留\n第二行 ",
        tags=[" alpha ", "alpha"],
    )
    assert created.status_code == 200
    clip = created.json()
    assert clip["content"] == "  原文保留\n第二行 "
    assert clip["tags"] == ["alpha"]
    assert len(clip["content_hash"]) == 64
    clip_id = clip["clip_id"]

    read = client.get(f"/api/knowledge/clips/{clip_id}")
    assert read.status_code == 200
    assert read.json() == clip
    assert client.get("/api/knowledge/clips/missing").status_code == 404

    duplicate = _create(client, "  原文保留\n第二行 ", tags=[" alpha ", "alpha"])
    assert duplicate.status_code == 200
    assert duplicate.json()["clip_id"] == clip_id
    assert len(client.get("/api/knowledge/clips").json()["clips"]) == 1

    conflict = _create(client, "不同内容")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "request_id_conflict"

    updated = client.post(
        f"/api/knowledge/clips/{clip_id}/update",
        json={"content": "owner edited", "tags": [" beta "], "source": "cli"},
        headers={"Origin": ORIGIN},
    )
    assert updated.status_code == 200
    assert updated.json()["content"] == "owner edited"
    assert updated.json()["tags"] == ["beta"]
    assert updated.json()["source"] == "cli"
    assert updated.json()["content_hash"] != clip["content_hash"]

    for path in (
        "/api/knowledge/clips?query=edited",
        "/api/knowledge/clips?q=beta",
        "/api/knowledge/search?query=edited",
        "/api/knowledge/search?q=beta",
    ):
        found = client.get(path)
        assert found.status_code == 200
        assert [item["clip_id"] for item in found.json()["clips"]] == [clip_id]
    assert client.get("/api/knowledge/clips?query=absent").json()["clips"] == []

    archived = client.post(
        f"/api/knowledge/clips/{clip_id}/archive", headers={"Origin": ORIGIN}
    )
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    assert client.get("/api/knowledge/clips").json()["clips"] == []
    assert (
        len(client.get("/api/knowledge/clips?include_archived=1").json()["clips"]) == 1
    )
    restored = client.post(
        f"/api/knowledge/clips/{clip_id}/unarchive", headers={"Origin": ORIGIN}
    )
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    assert len(client.get("/api/knowledge/clips").json()["clips"]) == 1


def test_invalid_input_is_rejected_without_echoing_content(tmp_path: Path) -> None:
    client = _client(tmp_path)
    cases: list[dict[str, object]] = [
        {"content": "", "request_id": "bad-1"},
        {"content": "   ", "request_id": "bad-2"},
        {"content": "x" * 100_001, "request_id": "bad-3"},
        {"content": "x", "tags": ["t" * 65], "request_id": "bad-4"},
        {
            "content": "x",
            "tags": [str(index) for index in range(33)],
            "request_id": "bad-5",
        },
        {"content": "x", "request_id": "r" * 201},
        {"content": "x", "source": "s" * 41, "request_id": "bad-6"},
        {"content": "x", "unexpected": True, "request_id": "bad-7"},
    ]
    for payload in cases:
        response = client.post(
            "/api/knowledge/clips", json=payload, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 422, payload.keys()
        assert response.json() == {"error": "invalid_knowledge_request"}
        assert "unexpected" not in response.text

    malformed = client.post(
        "/api/knowledge/clips",
        content=b"not json",
        headers={"Origin": ORIGIN, "content-type": "application/json"},
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"] == "invalid_json"
    wrong_type = client.post(
        "/api/knowledge/clips",
        content="text",
        headers={"Origin": ORIGIN, "content-type": "text/plain"},
    )
    assert wrong_type.status_code == 415

    created = _create(client, "valid", request_id="valid-update")
    clip_id = created.json()["clip_id"]
    empty_update = client.post(
        f"/api/knowledge/clips/{clip_id}/update",
        json={},
        headers={"Origin": ORIGIN},
    )
    assert empty_update.status_code == 422
    blank_update = client.post(
        f"/api/knowledge/clips/{clip_id}/update",
        json={"content": " "},
        headers={"Origin": ORIGIN},
    )
    assert blank_update.status_code == 422
    for query in ("limit=0", "limit=201", "offset=-1", "limit=nope"):
        response = client.get("/api/knowledge/clips?" + query)
        assert response.status_code == 422, query
        assert response.json()["error"] == "invalid_knowledge_request"


def test_read_only_snapshot_mode_exposes_no_knowledge_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = WebConfig(
        workspace=workspace,
        sessions_dir=tmp_path / "sessions",
        tasks_dir=tmp_path / "tasks",
        knowledge_dir=tmp_path / "knowledge",
        web_root=None,
    )
    client = TestClient(create_app(config), base_url=ORIGIN)
    assert client.get("/api/knowledge/clips").status_code == 404
    assert not (tmp_path / "knowledge").exists()


def test_clip_is_readable_after_an_app_restart(tmp_path: Path) -> None:
    first = _client(tmp_path)
    created = _create(first, "survives restart", request_id="restart-api").json()

    restarted = _client(tmp_path)
    response = restarted.get("/api/knowledge/clips/" + created["clip_id"])
    assert response.status_code == 200
    assert response.json()["content"] == "survives restart"
