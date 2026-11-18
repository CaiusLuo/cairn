"""Steering through the shared Conversation service.

A steer is accepted only while its Run is executing, is recorded before the API
acknowledges it, joins that same turn, and is reported as unapplied instead of
being silently dropped when the turn ends first.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, Message
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository

WAIT_SECONDS = 5.0


class GatedModel(OfflineDevelopmentModel):
    """Blocks its first call so a steer can arrive while the turn is running."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.gate = asyncio.Event()

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del tools
        self.calls.append(
            [message.content or "" for message in messages if message.role == "user"]
        )
        if len(self.calls) == 1:
            await self.gate.wait()
        return LLMResponse(content=f"reply:{len(self.calls)}")


async def wait_until(predicate: Any) -> None:
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


async def wait_terminal(api: RuntimeClient, run_id: str) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while True:
        run = await api.get_run(run_id)
        if run["status"] not in {"queued", "running"}:
            return run
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"Run {run_id} did not settle: {run['status']}")
        await asyncio.sleep(0.01)


def app_with_model(tmp_path: Path, model: GatedModel) -> Any:
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
    app.state.execution.llm_factory = lambda: model
    return app


def client(app: Any) -> RuntimeClient:
    return RuntimeClient(
        "http://127.0.0.1:8777",
        "zzzz",
        transport=httpx.ASGITransport(app=app),
    )


def test_steer_joins_the_running_turn_and_is_confirmed(tmp_path: Path) -> None:
    model = GatedModel()
    app = app_with_model(tmp_path, model)
    api = client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            run = await api.send_conversation(
                conversation["conversation_id"],
                "start",
                source="cli",
                request_id="steer-once",
            )
            await wait_until(lambda: len(model.calls) == 1)

            receipt = await api.steer_run(run["run_id"], "use the other module")

            assert receipt == {
                "accepted": True,
                "run_id": run["run_id"],
                "status": "running",
                "pending": 1,
            }
            model.gate.set()
            settled = await wait_terminal(api, run["run_id"])
            assert settled["status"] == "completed", settled
            assert settled["result"] == "reply:2"
            assert model.calls[-1] == ["start", "use the other module"]

            events = (await api.run_events(run["run_id"]))["events"]
            steer_events = [
                (event["kind"], event["data"].get("text"))
                for event in events
                if event["kind"].startswith("steer_")
            ]
            assert steer_events == [
                ("steer_accepted", "use the other module"),
                ("steer_applied", "use the other module"),
            ]
            history = await api.conversation_history(conversation["conversation_id"])
            assert [
                message["content"] for message in history if message["role"] == "user"
            ] == ["start", "use the other module"]

    asyncio.run(scenario())


def test_steer_is_rejected_once_the_turn_settled(tmp_path: Path) -> None:
    model = GatedModel()
    model.gate.set()
    app = app_with_model(tmp_path, model)
    api = client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            run = await api.send_conversation(
                conversation["conversation_id"],
                "start",
                source="cli",
                request_id="steer-late",
            )
            assert (await wait_terminal(api, run["run_id"]))["status"] == "completed"

            with pytest.raises(httpx.HTTPStatusError) as rejected:
                await api.steer_run(run["run_id"], "too late")

            assert rejected.value.response.status_code == 409
            assert rejected.value.response.json() == {"error": "run_not_steerable"}

    asyncio.run(scenario())


def test_steer_accepted_before_cancellation_is_reported_unapplied(
    tmp_path: Path,
) -> None:
    model = GatedModel()
    app = app_with_model(tmp_path, model)
    api = client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            run = await api.send_conversation(
                conversation["conversation_id"],
                "start",
                source="cli",
                request_id="steer-cancel",
            )
            await wait_until(lambda: len(model.calls) == 1)
            await api.steer_run(run["run_id"], "never applied")

            await api.cancel_run(run["run_id"])
            settled = await api.get_run(run["run_id"])
            assert settled["status"] in {"cancelled", "recovery_required"}, settled

            events = (await api.run_events(run["run_id"]))["events"]
            assert any(
                event["kind"] == "steer_unapplied"
                and event["data"].get("text") == "never applied"
                for event in events
            )
            assert model.calls == [["start"]]

    asyncio.run(scenario())
