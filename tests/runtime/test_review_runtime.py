"""The Server runtime wires the review service into its authenticated Web app."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from cairn.agent.budget import RunBudget
from cairn.server.bootstrap import runtime_app
from tests.support.git import initialize_repository
from tests.support.pg import database_for


def test_runtime_bootstrap_exposes_reviews_and_skips_empty_periods(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    app = runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="review-runtime-token",
        database=database_for(tmp_path),
    )
    assert app.state.reviews is not None
    assert app.state.weekly_reviews is not None
    headers = {
        "Authorization": "Bearer review-runtime-token",
        "host": "127.0.0.1:8777",
    }
    with TestClient(app, base_url="http://127.0.0.1:8777", headers=headers) as client:
        assert client.get("/api/knowledge/reviews").json() == {"reviews": []}

        created = client.post(
            "/api/knowledge/reviews/daily",
            json={"date": "2026-01-15", "request_id": "runtime-empty-day"},
            headers={"Origin": "http://127.0.0.1:8777"},
        )
        assert created.status_code == 409
        assert created.json() == {"error": "review_no_content"}

        weekly = client.post(
            "/api/knowledge/reviews/weekly",
            json={"week_id": "2026-W04", "request_id": "runtime-empty-week"},
            headers={"Origin": "http://127.0.0.1:8777"},
        )
        assert weekly.status_code == 409
        assert weekly.json() == {"error": "review_no_content"}

        assert client.get("/api/knowledge/reviews").json() == {"reviews": []}
