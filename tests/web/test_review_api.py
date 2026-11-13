"""Authenticated review API returns queryable, non-blocking daily reports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.testclient import TestClient

from cairn.agent.models.messages import LLMResponse, Message
from cairn.interfaces.web.app import create_app
from cairn.interfaces.web.config import WebConfig
from cairn.knowledge.store import KnowledgeStore
from cairn.review.models import (
    ReviewInput,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
    SourceSnapshot,
    SourceTrust,
)
from cairn.review.service import DailyReviewService
from cairn.review.store import ReviewStore

PASSPHRASE = "review-passphrase"
ORIGIN = "http://127.0.0.1"
DAY = date(2026, 1, 15)
TZ = "Asia/Shanghai"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _review_input() -> ReviewInput:
    snapshot = SourceSnapshot(
        source_type="development_run",
        source_id="run-1",
        content_hash=_hash("run"),
        occurred_at="2026-01-15T10:00:00+08:00",
        trust=SourceTrust.SYSTEM_RECEIPT,
        may_prove_completion=True,
        verification="run_status=completed",
        snapshot='{"run_id":"run-1","status":"completed"}',
    )
    start = datetime(DAY.year, DAY.month, DAY.day, tzinfo=ZoneInfo(TZ))
    return ReviewInput.build(
        review_type=ReviewType.DAILY,
        day=DAY,
        timezone=TZ,
        period_start=start,
        period_end=start + timedelta(days=1),
        sources=(snapshot,),
        collection_status=(
            SourceCollectionStatus(
                source_type="development_run",
                status=SourceCollectionState.OK,
                discovered=1,
                included=1,
            ),
        ),
        budget_chars=60_000,
    )


def _payload(overview: str) -> str:
    return json.dumps(
        {
            "date": DAY.isoformat(),
            "overview": overview,
            "completed_activities": [
                {"text": "Completed the release check.", "source_refs": ["run-1"]}
            ],
            "knowledge_insights": [],
            "problems": [],
            "recommendations": [],
            "source_refs": ["run-1"],
            "confidence_notes": [],
        }
    )


class FakeCollector:
    def __init__(self, review_input: ReviewInput) -> None:
        self.review_input = review_input

    def collect_daily(self, day: date, timezone: str) -> ReviewInput:
        return self.review_input


class BlockingLLM:
    def __init__(self, gate: threading.Event, responses: list[str]) -> None:
        self.gate = gate
        self.responses = responses
        self.calls = 0

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls += 1
        await asyncio.to_thread(self.gate.wait)
        return LLMResponse(content=self.responses.pop(0))


def _config(tmp_path: Path, *, auth: bool = True) -> WebConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return WebConfig(
        workspace=workspace,
        sessions_dir=tmp_path / "sessions",
        tasks_dir=tmp_path / "tasks",
        knowledge_dir=tmp_path / "knowledge",
        web_root=None,
        auth_passphrase=PASSPHRASE if auth else "",
    )


def _app(
    tmp_path: Path,
    *,
    llm: BlockingLLM | None = None,
    auth: bool = True,
) -> tuple[Starlette, DailyReviewService, KnowledgeStore]:
    store = KnowledgeStore(tmp_path / "knowledge")
    service = DailyReviewService(
        store=ReviewStore(store),
        collector=FakeCollector(_review_input()),
        llm=llm,
        model_id="test-model",
    )
    web = create_app(
        _config(tmp_path, auth=auth),
        runtime_token="host-token" if auth else None,
        knowledge_store=store,
        reviews=service,
    )
    return web, service, store


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/login",
        json={"username": "cairn", "auth_passphrase": PASSPHRASE},
    )
    assert response.status_code == 200


def _wait_completed(client: TestClient, review_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/knowledge/reviews/{review_id}")
        assert response.status_code == 200
        detail = response.json()
        if detail["status"] in {"completed", "failed"}:
            return cast(dict[str, Any], detail)
        time.sleep(0.02)
    raise AssertionError("review did not settle")


def test_review_api_requires_auth_and_same_origin(tmp_path: Path) -> None:
    gate = threading.Event()
    web, _service, _store = _app(tmp_path, llm=BlockingLLM(gate, [_payload("ok")]))
    with TestClient(web, base_url=ORIGIN) as client:
        assert client.get("/api/knowledge/reviews").status_code == 401
        _login(client)
        assert client.get("/api/knowledge/reviews").json() == {"reviews": []}

        payload = {"date": DAY.isoformat(), "request_id": "api-1"}
        assert (
            client.post("/api/knowledge/reviews/daily", json=payload).status_code == 403
        )
        assert (
            client.post(
                "/api/knowledge/reviews/daily",
                content=json.dumps(payload),
                headers={"Origin": ORIGIN, "content-type": "text/plain"},
            ).status_code
            == 415
        )
        bad_json = client.post(
            "/api/knowledge/reviews/daily",
            content="{",
            headers={"Origin": ORIGIN, "content-type": "application/json"},
        )
        assert bad_json.status_code == 400
        invalid = client.post(
            "/api/knowledge/reviews/daily",
            json={"date": DAY.isoformat()},
            headers={"Origin": ORIGIN},
        )
        assert invalid.status_code == 422
        unknown_zone = client.post(
            "/api/knowledge/reviews/daily",
            json={"date": DAY.isoformat(), "request_id": "x", "timezone": "Bad/Zone"},
            headers={"Origin": ORIGIN},
        )
        assert unknown_zone.status_code == 422
        assert client.get("/api/knowledge/reviews/missing").status_code == 404
        assert client.get("/api/knowledge/reviews?type=weekly").json() == {
            "reviews": []
        }
        assert client.get("/api/knowledge/reviews?type=bad").status_code == 422
        assert client.get("/api/knowledge/reviews?limit=0").status_code == 422
        gate.set()


def test_create_poll_list_detail_replay_and_regenerate(tmp_path: Path) -> None:
    gate = threading.Event()
    llm = BlockingLLM(gate, [_payload("First report."), _payload("Second report.")])
    web, _service, _store = _app(tmp_path, llm=llm)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        payload = {
            "date": DAY.isoformat(),
            "timezone": TZ,
            "request_id": "api-1",
        }
        created = client.post(
            "/api/knowledge/reviews/daily",
            json=payload,
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 202
        body = created.json()
        assert body["review_id"].startswith("review-")
        assert body["status"] in {"pending", "generating"}
        review_id = body["review_id"]

        gate.set()
        detail = _wait_completed(client, review_id)
        assert detail["status"] == "completed"
        assert detail["overview"] == "First report."
        assert detail["summary"]["completed_activities"][0]["source_refs"] == ["run-1"]
        assert detail["source_manifest"][0]["source_id"] == "run-1"
        assert detail["collection_status"][0]["source_type"] == "development_run"
        assert "First report." in detail["summary_markdown"]

        listing = client.get("/api/knowledge/reviews").json()["reviews"]
        assert [item["review_id"] for item in listing] == [review_id]
        assert listing[0]["status"] == "completed"

        replay = client.post(
            "/api/knowledge/reviews/daily",
            json=payload,
            headers={"Origin": ORIGIN},
        )
        assert replay.status_code == 202
        assert replay.json()["review_id"] == review_id
        assert replay.json()["status"] == "completed"
        assert llm.calls == 1

        conflict = client.post(
            "/api/knowledge/reviews/daily",
            json={**payload, "regenerate": True},
            headers={"Origin": ORIGIN},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"] == "request_id_conflict"

        regenerated = client.post(
            "/api/knowledge/reviews/daily",
            json={**payload, "request_id": "api-2", "regenerate": True},
            headers={"Origin": ORIGIN},
        )
        assert regenerated.status_code == 202
        second_id = regenerated.json()["review_id"]
        assert second_id != review_id
        second = _wait_completed(client, second_id)
        assert second["overview"] == "Second report."
        assert second["version"] == 2
        assert second["supersedes_review_id"] == review_id
        assert len(client.get("/api/knowledge/reviews").json()["reviews"]) == 2


def test_read_only_mode_does_not_expose_review_routes(tmp_path: Path) -> None:
    web, _service, _store = _app(tmp_path, auth=False)
    with TestClient(web, base_url=ORIGIN) as client:
        assert client.get("/api/knowledge/reviews").status_code == 404
        assert (
            client.post(
                "/api/knowledge/reviews/daily",
                json={"date": DAY.isoformat(), "request_id": "read-only"},
                headers={"Origin": ORIGIN},
            ).status_code
            == 404
        )


def test_completed_report_is_readable_from_a_new_app(tmp_path: Path) -> None:
    gate = threading.Event()
    gate.set()
    web, _service, _store = _app(
        tmp_path, llm=BlockingLLM(gate, [_payload("Persisted report.")])
    )
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        created = client.post(
            "/api/knowledge/reviews/daily",
            json={
                "date": DAY.isoformat(),
                "request_id": "persisted",
            },
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 202
        review_id = created.json()["review_id"]
        assert _wait_completed(client, review_id)["status"] == "completed"

    restarted, _restarted_service, _store = _app(tmp_path)
    with TestClient(restarted, base_url=ORIGIN) as client:
        _login(client)
        detail = client.get(f"/api/knowledge/reviews/{review_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "completed"
        assert detail.json()["overview"] == "Persisted report."
