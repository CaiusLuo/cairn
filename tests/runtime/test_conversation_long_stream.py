"""Long streaming output and WebSocket reconnect reconstruction."""

from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from cairn.agent.budget import RunBudget
from cairn.agent.models.client import LLMStreamChunk
from cairn.agent.models.messages import LLMUsage
from cairn.server import runtime_app
from tests.support.git import initialize_repository
from tests.support.streaming import ScriptedStreamLLM, StreamBarrier


def test_ten_thousand_character_stream_survives_reconnect(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    entered, release = asyncio.Event(), asyncio.Event()
    model = ScriptedStreamLLM(
        [
            StreamBarrier(entered, release),
            *(LLMStreamChunk(text="x" * 100) for _ in range(100)),
            LLMStreamChunk(
                finish_reason="stop", usage=LLMUsage(input_tokens=20, output_tokens=10)
            ),
        ]
    )
    app = runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="zzzz",
    )
    app.state.execution.llm_factory = lambda: model
    headers = {"Authorization": "Bearer zzzz", "host": "127.0.0.1:8777"}
    with TestClient(app, base_url="http://127.0.0.1:8777", headers=headers) as client:
        conversation = client.post(
            "/api/runtime/conversations",
            json={"prompt": "long", "source": "web"},
        ).json()
        run = client.post(
            f"/api/runtime/conversations/{conversation['conversation_id']}/messages",
            json={"prompt": "long", "source": "web", "request_id": "long-stream"},
        ).json()
        assert client.portal is not None
        client.portal.call(asyncio.wait_for, entered.wait(), 10)
        previews = ""
        cursor = 0
        released = False
        while True:
            closed = False
            with client.websocket_connect(
                f"/api/runtime/runs/{run['run_id']}/stream?after={cursor}",
                headers=headers,
            ) as socket:
                while True:
                    try:
                        message = socket.receive_json()
                    except Exception:
                        closed = True
                        break
                    if message["type"] == "ready":
                        cursor = max(cursor, int(message["cursor"]))
                        if not released:
                            client.portal.call(release.set)
                            released = True
                    elif message["type"] == "event":
                        event = message["event"]
                        cursor = max(cursor, int(event["sequence"]))
                        if event["kind"] == "model_text_delta":
                            previews += event["data"].get("text", "")
                    elif message["type"] == "preview":
                        previews += message["text"]
                    elif message["type"] == "preview_snapshot":
                        previews = message["text"]
                    elif message["type"] == "resync":
                        closed = True
                        break
                    elif message["type"] == "settled":
                        break
            if not closed:
                break
        assert previews == "x" * 10_000
        # The same run is fully replayable after the live socket is gone.
        with client.websocket_connect(
            f"/api/runtime/runs/{run['run_id']}/stream?after=0", headers=headers
        ) as socket:
            events: list[dict[str, object]] = []
            while True:
                message = socket.receive_json()
                if message["type"] == "event":
                    events.append(message["event"])
                elif message["type"] == "settled":
                    break
            assert len(events) >= 1
            sequences: list[int] = []
            for event in events:
                sequence = event["sequence"]
                assert isinstance(sequence, int)
                sequences.append(sequence)
            assert all(sequence > 0 for sequence in sequences)
            assert sorted(sequences) == sequences
        history = client.get(
            f"/api/runtime/conversations/{conversation['conversation_id']}/history"
        ).json()
        assert history[-1]["content"] == "x" * 10_000


def test_websocket_disconnect_mid_stream_resumes_without_duplicate_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    entered, release = asyncio.Event(), asyncio.Event()
    model = ScriptedStreamLLM(
        [
            LLMStreamChunk(text="A" * 100),
            StreamBarrier(entered, release),
            LLMStreamChunk(text="B" * 100),
            LLMStreamChunk(
                finish_reason="stop", usage=LLMUsage(input_tokens=20, output_tokens=10)
            ),
        ]
    )
    app = runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="zzzz",
    )
    app.state.execution.llm_factory = lambda: model
    headers = {"Authorization": "Bearer zzzz", "host": "127.0.0.1:8777"}
    with TestClient(app, base_url="http://127.0.0.1:8777", headers=headers) as client:
        conversation = client.post(
            "/api/runtime/conversations",
            json={"prompt": "disconnect", "source": "web"},
        ).json()
        run = client.post(
            f"/api/runtime/conversations/{conversation['conversation_id']}/messages",
            json={
                "prompt": "disconnect",
                "source": "web",
                "request_id": "disconnect-stream",
            },
        ).json()
        assert client.portal is not None
        client.portal.call(asyncio.wait_for, entered.wait(), 10)
        snapshot = ""
        with client.websocket_connect(
            f"/api/runtime/runs/{run['run_id']}/stream", headers=headers
        ) as socket:
            while True:
                message = socket.receive_json()
                if message["type"] == "preview_snapshot":
                    snapshot = message["text"]
                    break
                if message["type"] == "preview":
                    snapshot += message["text"]
                    break
        assert snapshot and set(snapshot) == {"A"}
        client.portal.call(release.set)
        for _ in range(100):
            record = client.get(f"/api/runtime/runs/{run['run_id']}").json()
            if record["status"] not in {"queued", "running"}:
                break
            import time

            time.sleep(0.02)
        assert record["status"] == "completed", record
        # A reconnecting consumer gets the complete persisted turn exactly once.
        with client.websocket_connect(
            f"/api/runtime/runs/{run['run_id']}/stream?after=0", headers=headers
        ) as socket:
            event_text = ""
            while True:
                message = socket.receive_json()
                if message["type"] == "event":
                    event = message["event"]
                    if event["kind"] == "model_text_delta":
                        event_text += event["data"].get("text", "")
                elif message["type"] == "settled":
                    break
        assert event_text.startswith(snapshot)
        assert event_text.count("A") > 0 and event_text.count("B") > 0
        history = client.get(
            f"/api/runtime/conversations/{conversation['conversation_id']}/history"
        ).json()
        assert history[-1]["content"] == event_text
