"""The Server runtime wires the review service into its authenticated Web app."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from cairn.agent.budget import RunBudget
from cairn.review.service import NO_SOURCE_MESSAGES
from cairn.server.bootstrap import runtime_app
from tests.support.git import initialize_repository


def test_runtime_bootstrap_exposes_reviews_and_finishes_empty_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review collection needs no WAL store, but the full runtime lifespan opens
    # the session store. Local development SQLite builds use the same explicit
    # declaration as the other focused runtime tests.
    monkeypatch.setenv("CAIRN_ALLOW_UNVERIFIED_SQLITE", "1")
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    app = runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="review-runtime-token",
    )
    assert app.state.reviews is not None
    headers = {
        "Authorization": "Bearer review-runtime-token",
        "host": "127.0.0.1:8777",
    }
    with TestClient(app, base_url="http://127.0.0.1:8777", headers=headers) as client:
        listing = client.get("/api/knowledge/reviews")
        assert listing.status_code == 200
        assert listing.json() == {"reviews": []}

        created = client.post(
            "/api/knowledge/reviews/daily",
            json={"date": "2026-01-15", "request_id": "runtime-empty-day"},
            headers={"Origin": "http://127.0.0.1:8777"},
        )
        assert created.status_code == 202
        body = created.json()
        assert body["status"] == "completed"
        assert body["overview"] in NO_SOURCE_MESSAGES

        detail = client.get(f"/api/knowledge/reviews/{body['review_id']}")
        assert detail.status_code == 200
        assert detail.json()["summary_markdown"]
        assert body["overview"] in detail.json()["summary_markdown"]
