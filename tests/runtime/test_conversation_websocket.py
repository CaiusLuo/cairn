"""Conversation WebSocket replay must paginate and resume from a cursor."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from starlette.testclient import TestClient

from cairn.core.budget import RunBudget
from cairn.core.domain import ConversationSource, RunStatus
from cairn.server import runtime_app
from tests.support.git import initialize_repository


def _seed_events(app: Any, source: Path) -> str:
    store = app.state.conversations.store
    conversation = store.create_conversation(
        workspace=source,
        source=ConversationSource.CLI,
        initial_prompt="events",
    )
    run = store.accept_run(
        conversation_id=conversation.conversation_id,
        prompt="events",
        source=ConversationSource.CLI,
        request_id="events-request",
    )
    for index in range(550):
        store.append_run_event(run.run_id, "agent_step", {"index": index})
    store.update_run(run.run_id, status=RunStatus.COMPLETED, result="done")
    return cast(str, run.run_id)


def test_websocket_replays_all_pages_and_reconnects_from_cursor(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    app = runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="zzzz",
    )
    run_id = _seed_events(app, source)
    headers = {"Authorization": "Bearer zzzz", "host": "127.0.0.1:8777"}
    with TestClient(app, base_url="http://127.0.0.1:8777", headers=headers) as client:
        with client.websocket_connect(
            f"/api/runtime/runs/{run_id}/stream?after=0", headers=headers
        ) as socket:
            first = []
            while True:
                message = socket.receive_json()
                if message["type"] == "event":
                    first.append(message["event"]["sequence"])
                elif message["type"] == "ready":
                    assert message["cursor"] == 0
                    assert message["last_sequence"] == 550
                elif message["type"] == "settled":
                    assert message["cursor"] == 550
                    break
            assert first == list(range(1, 551))
        with client.websocket_connect(
            f"/api/runtime/runs/{run_id}/stream?after=250", headers=headers
        ) as socket:
            replayed = []
            while True:
                message = socket.receive_json()
                if message["type"] == "event":
                    replayed.append(message["event"]["sequence"])
                elif message["type"] == "settled":
                    assert message["cursor"] == 550
                    break
            assert replayed == list(range(251, 551))


def test_http_event_pages_and_run_cursor_cover_backlog(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    app = runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="zzzz",
    )
    run_id = _seed_events(app, source)
    headers = {"Authorization": "Bearer zzzz", "host": "127.0.0.1:8777"}
    with TestClient(app, base_url="http://127.0.0.1:8777", headers=headers) as client:
        cursor = 0
        sequences: list[int] = []
        while True:
            page = client.get(
                f"/api/runtime/runs/{run_id}/events?after={cursor}"
            ).json()
            events = page["events"]
            if not events:
                break
            sequences.extend(event["sequence"] for event in events)
            cursor = page["cursor"]
        assert sequences == list(range(1, 551))
        record = client.get(f"/api/runtime/runs/{run_id}").json()
        assert record["cursor"] == 550
