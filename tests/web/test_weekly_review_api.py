"""Authenticated weekly review creation, listing and manual retry API."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as wall_time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

from cairn.agent.models.messages import LLMResponse, Message
from cairn.interfaces.web.app import create_app
from cairn.interfaces.web.config import WebConfig
from cairn.interfaces.web.reviews import review_routes
from cairn.knowledge.store import KnowledgeStore
from cairn.review.models import (
    DeliveryTarget,
    ReviewInput,
    ReviewType,
    SourceCollectionState,
    SourceCollectionStatus,
    SourceSnapshot,
    SourceTrust,
)
from cairn.review.service import DailyReviewService, ReviewNoContent
from cairn.review.store import ReviewStore
from cairn.review.weekly import WeeklyReviewService, week_period
from tests.support.pg import database_for

PASSPHRASE = "weekly-passphrase"
ORIGIN = "http://127.0.0.1"
WEEK = "2026-W03"
TZ = "Asia/Shanghai"


class NoopDailyCollector:
    """A daily input with one source, so manual daily generation is accepted."""

    def collect_daily(
        self, day: date, timezone: str, *, cutoff: Any = None
    ) -> ReviewInput:
        zone = ZoneInfo(timezone)
        if cutoff is None:
            start = datetime.combine(day, wall_time.min, tzinfo=zone)
            end = start + timedelta(days=1)
        else:
            end = datetime.combine(day, cutoff, tzinfo=zone)
            start = end - timedelta(days=1)
        source = SourceSnapshot(
            source_type="development_task",
            source_id="task-1",
            content_hash="a" * 64,
            occurred_at=start.isoformat(),
            trust=SourceTrust.SYSTEM_RECEIPT,
            may_prove_completion=True,
            verification="business_status=done",
            snapshot='{"task_id":"task-1"}',
        )
        return ReviewInput.build(
            review_type=ReviewType.DAILY,
            day=day,
            timezone=timezone,
            period_start=start,
            period_end=end,
            sources=(source,),
            collection_status=(
                SourceCollectionStatus(
                    source_type="development_task",
                    status=SourceCollectionState.OK,
                    discovered=1,
                    included=1,
                ),
            ),
            budget_chars=60_000,
        )


class FakeWeeklyCollector:
    def __init__(self, review_input: ReviewInput) -> None:
        self.review_input = review_input

    def collect_weekly(self, period: Any, timezone: str) -> ReviewInput:
        return self.review_input


class BlockingLLM:
    def __init__(self, gate: threading.Event, content: str) -> None:
        self.gate = gate
        self.content = content
        self.calls = 0

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        self.calls += 1
        await asyncio.to_thread(self.gate.wait)
        return LLMResponse(content=self.content)


def _payload(period: Any) -> str:
    return json.dumps(
        {
            "week_id": period.week_id,
            "period_start": period.period_start.isoformat(),
            "period_end": period.period_end.isoformat(),
            "overview": "A weekly API report.",
            "major_events": [{"text": "One task.", "source_refs": ["task-1"]}],
            "progress": [{"text": "Finished it.", "source_refs": ["task-1"]}],
            "knowledge_insights": [],
            "recurring_problems": [],
            "next_week_priorities": [],
            "source_refs": ["task-1"],
            "confidence_notes": [],
        }
    )


def _app(
    tmp_path: Path, gate: threading.Event
) -> tuple[Any, WeeklyReviewService, ReviewStore]:
    period = week_period(WEEK, timezone=TZ)
    source = SourceSnapshot(
        source_type="development_task",
        source_id="task-1",
        content_hash="a" * 64,
        occurred_at=period.period_end.isoformat(),
        trust=SourceTrust.SYSTEM_RECEIPT,
        may_prove_completion=True,
        verification="business_status=done",
        snapshot='{"task_id":"task-1"}',
    )
    weekly_input = ReviewInput.build(
        review_type=ReviewType.WEEKLY,
        day=period.day,
        timezone=TZ,
        period_start=period.period_start,
        period_end=period.period_end,
        sources=(source,),
        collection_status=(
            SourceCollectionStatus(
                source_type="development_task",
                status=SourceCollectionState.OK,
                discovered=1,
                included=1,
            ),
        ),
        budget_chars=200_000,
    )
    knowledge = KnowledgeStore(database_for(tmp_path))
    store = ReviewStore(knowledge)
    daily = DailyReviewService(
        store=store,
        collector=NoopDailyCollector(),
        llm=None,
        delivery_target=DeliveryTarget(recipient="owner"),
    )
    weekly = WeeklyReviewService(
        store=store,
        collector=FakeWeeklyCollector(weekly_input),
        llm=BlockingLLM(gate, _payload(period)),
        model_id="weekly-api-model",
        delivery_target=DeliveryTarget(recipient="owner"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = WebConfig(
        workspace=workspace,
        tasks_dir=tmp_path / "tasks",
        knowledge_dir=tmp_path / "knowledge",
        web_root=None,
        auth_passphrase=PASSPHRASE,
    )
    web = create_app(
        config,
        runtime_token="host-token",
        knowledge_store=knowledge,
        database=database_for(tmp_path),
    )
    web.router.routes[0:0] = review_routes(daily, "host-token", weekly)
    return web, weekly, store


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
    raise AssertionError("weekly review did not settle")


def test_weekly_api_requires_auth_and_same_origin(tmp_path: Path) -> None:
    gate = threading.Event()
    web, _weekly, _store = _app(tmp_path, gate)
    with TestClient(web, base_url=ORIGIN) as client:
        assert client.get("/api/knowledge/reviews").status_code == 401
        _login(client)
        assert client.get("/api/knowledge/reviews?type=weekly").json() == {
            "reviews": []
        }
        payload = {"week_id": WEEK, "request_id": "weekly-api-1"}
        assert (
            client.post("/api/knowledge/reviews/weekly", json=payload).status_code
            == 403
        )
        assert (
            client.post(
                "/api/knowledge/reviews/weekly",
                json={"week_id": "2026-W99", "request_id": "bad-week"},
                headers={"Origin": ORIGIN},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/knowledge/reviews/weekly",
                json={"week_id": WEEK},
                headers={"Origin": ORIGIN},
            ).status_code
            == 422
        )
        gate.set()


def test_weekly_review_create_list_detail_and_replay(tmp_path: Path) -> None:
    gate = threading.Event()
    web, _weekly, _store = _app(tmp_path, gate)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        created = client.post(
            "/api/knowledge/reviews/weekly",
            json={"week_id": WEEK, "request_id": "weekly-api-2", "timezone": TZ},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 202
        body = created.json()
        assert body["review_type"] == "weekly"
        assert body["week_id"] == WEEK
        assert body["status"] in {"pending", "generating"}

        gate.set()
        detail = _wait_completed(client, body["review_id"])
        assert detail["status"] == "completed"
        assert detail["week_id"] == WEEK
        assert detail["summary"]["week_id"] == WEEK
        assert detail["summary"]["progress"][0]["source_refs"] == ["task-1"]
        assert detail["summary_markdown"]

        listing = client.get("/api/knowledge/reviews?type=weekly").json()
        assert [item["review_id"] for item in listing["reviews"]] == [body["review_id"]]
        assert client.get("/api/knowledge/reviews?type=daily").json() == {"reviews": []}

        replay = client.post(
            "/api/knowledge/reviews/weekly",
            json={"week_id": WEEK, "request_id": "weekly-api-2", "timezone": TZ},
            headers={"Origin": ORIGIN},
        )
        assert replay.status_code == 202
        assert replay.json()["review_id"] == body["review_id"]


def test_review_history_filters_by_day_and_iso_week(tmp_path: Path) -> None:
    gate = threading.Event()
    web, _weekly, _store = _app(tmp_path, gate)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        created_daily = client.post(
            "/api/knowledge/reviews/daily",
            json={
                "date": "2026-01-15",
                "timezone": TZ,
                "request_id": "filter-daily",
            },
            headers={"Origin": ORIGIN},
        )
        assert created_daily.status_code == 202
        created_weekly = client.post(
            "/api/knowledge/reviews/weekly",
            json={"week_id": WEEK, "timezone": TZ, "request_id": "filter-weekly"},
            headers={"Origin": ORIGIN},
        )
        assert created_weekly.status_code == 202
        gate.set()
        _wait_completed(client, created_weekly.json()["review_id"])

        daily = client.get("/api/knowledge/reviews?type=daily&day=2026-01-15").json()[
            "reviews"
        ]
        assert [item["review_id"] for item in daily] == [
            created_daily.json()["review_id"]
        ]
        weekly = client.get(
            f"/api/knowledge/reviews?type=weekly&week_id={WEEK}"
        ).json()["reviews"]
        assert [item["review_id"] for item in weekly] == [
            created_weekly.json()["review_id"]
        ]
        assert client.get(
            "/api/knowledge/reviews?type=weekly&week_id=2026-W04"
        ).json() == {"reviews": []}
        assert client.get("/api/knowledge/reviews?day=not-a-date").status_code == 422
        assert (
            client.get(
                "/api/knowledge/reviews?day=2026-01-15&week_id=2026-W03"
            ).status_code
            == 422
        )


def test_weekly_review_rejects_a_period_that_has_not_ended(tmp_path: Path) -> None:
    gate = threading.Event()
    gate.set()
    web, _weekly, _store = _app(tmp_path, gate)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        response = client.post(
            "/api/knowledge/reviews/weekly",
            json={"week_id": "2099-W01", "request_id": "weekly-future"},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 409
        assert response.json() == {"error": "review_period_not_ended"}


def test_empty_week_returns_no_content_and_creates_no_review(
    tmp_path: Path,
) -> None:
    gate = threading.Event()
    gate.set()
    web, weekly, _store = _app(tmp_path, gate)
    period = week_period(WEEK, timezone=TZ)
    weekly.collector = FakeWeeklyCollector(
        ReviewInput.build(
            review_type=ReviewType.WEEKLY,
            day=period.day,
            timezone=TZ,
            period_start=period.period_start,
            period_end=period.period_end,
            sources=(),
            collection_status=(),
            budget_chars=200_000,
        )
    )
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        response = client.post(
            "/api/knowledge/reviews/weekly",
            json={"week_id": WEEK, "request_id": "weekly-empty"},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 409
        assert response.json() == {"error": "review_no_content"}
        assert client.get("/api/knowledge/reviews?type=weekly").json() == {
            "reviews": []
        }


def test_weekly_schedule_retry_entry_reports_disabled_and_not_failed(
    tmp_path: Path,
) -> None:
    gate = threading.Event()
    gate.set()
    web, _weekly, _store = _app(tmp_path, gate)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        disabled = client.post(
            "/api/knowledge/reviews/schedule/weekly/retry",
            json={"week_id": WEEK, "request_id": "retry-disabled"},
            headers={"Origin": ORIGIN},
        )
        assert disabled.status_code == 503
        assert disabled.json() == {"error": "weekly_review_schedule_disabled"}

        web.state.weekly_review_schedule = SimpleNamespace(
            retry_failed=lambda _week_id, _request_id: _none()
        )
        missing = client.post(
            "/api/knowledge/reviews/schedule/weekly/retry",
            json={"week_id": WEEK, "request_id": "retry-missing"},
            headers={"Origin": ORIGIN},
        )
        assert missing.status_code == 409
        assert missing.json() == {"error": "review_week_not_failed"}

        without_id = client.post(
            "/api/knowledge/reviews/schedule/weekly/retry",
            json={"week_id": WEEK},
            headers={"Origin": ORIGIN},
        )
        assert without_id.status_code == 422

        async def conflict(_week_id: str, _request_id: str) -> None:
            raise ValueError("retry request_id was used for another week")

        web.state.weekly_review_schedule = SimpleNamespace(retry_failed=conflict)
        reused_id = client.post(
            "/api/knowledge/reviews/schedule/weekly/retry",
            json={"week_id": WEEK, "request_id": "retry-reused"},
            headers={"Origin": ORIGIN},
        )
        assert reused_id.status_code == 409
        assert reused_id.json() == {"error": "retry_request_conflict"}

        async def no_content(_week_id: str, _request_id: str) -> None:
            raise ReviewNoContent

        web.state.weekly_review_schedule = SimpleNamespace(retry_failed=no_content)
        quiet = client.post(
            "/api/knowledge/reviews/schedule/weekly/retry",
            json={"week_id": WEEK, "request_id": "retry-empty"},
            headers={"Origin": ORIGIN},
        )
        assert quiet.status_code == 409
        assert quiet.json() == {"error": "review_no_content"}


async def _none() -> None:
    return None
