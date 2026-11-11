"""Cross-entry continuation of one explicit Development Task."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, Message
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.development.catalogue import (
    INPUT_DIAGNOSTIC_LIMIT,
    INPUT_EXCERPT_LIMIT,
    RuntimeCatalogue,
    RuntimeTask,
    TaskInput,
)
from cairn.development.service import TaskNeedsRecovery
from cairn.interfaces.cli.runtime_chat import RemoteConversation
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


class _SlowModel(OfflineDevelopmentModel):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        self.started.set()
        await self.release.wait()
        return await super().generate(messages, tools)


def _app(tmp_path: Path) -> Any:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    return runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="handoff-token",
    )


def _client(app: Any) -> RuntimeClient:
    return RuntimeClient(
        "http://127.0.0.1:8777",
        "handoff-token",
        transport=httpx.ASGITransport(app=app),
    )


def test_web_created_task_continues_from_cli_and_stays_observable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            entry = await api.create_development(
                "Create the shared development task", "web", "web-create-1"
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            created = await api.inspect(entry["task_id"])
            assert created["status"] == "completed"
            assert created["inputs"] == []
            assert len(app.state.runtime.list_tasks()) == 1

            cli = RemoteConversation(api, mode="development")
            history = await cli.select(entry["task_id"])
            assert [item["content"] for item in history if item["role"] == "user"] == [
                "Create the shared development task"
            ]
            accepted_sequence = created["sequence"]
            await cli.send("Continue from the CLI", request_id="cli-follow-1")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            final = await cli.refresh()
            assert final["task_id"] == entry["task_id"]
            assert final["session_id"] == created["session_id"]
            assert final["worktree"] == created["worktree"]
            assert final["status"] == "completed"

            (receipt,) = final["inputs"]
            assert receipt["request_id"] == "cli-follow-1"
            assert receipt["source"] == "cli"
            assert receipt["status"] == "completed"
            assert receipt["settled_sequence"] == final["sequence"]
            assert receipt["session_id"] == created["session_id"]
            assert receipt["worktree"] == created["worktree"]
            assert receipt["result_excerpt"]
            assert receipt["result_truncated"] is False

            # The Web view reads the same durable receipt and event sequence.
            observed = await api.inspect(entry["task_id"])
            assert observed["inputs"] == final["inputs"]
            page = await api.request("GET", f"tasks/{entry['task_id']}/events")
            accepted = [
                event for event in page["events"] if event["kind"] == "message_accepted"
            ]
            assert accepted[-1]["data"] == {
                "source": "cli",
                "request_id": "cli-follow-1",
            }
            settled = [
                event
                for event in page["events"]
                if event["sequence"] == receipt["settled_sequence"]
            ]
            assert settled and settled[0]["kind"] == "completed"

            # A replay of the accepted request is idempotent and does not execute.
            replay = await api.message(
                entry["task_id"],
                "Continue from the CLI",
                source="cli",
                request_id="cli-follow-1",
                expected_sequence=accepted_sequence,
            )
            assert replay["attempts"] == final["attempts"]
            assert replay["inputs"] == final["inputs"]

            # A distinct request ID appends another input without a second Task.
            await cli.send("Second CLI follow-up", request_id="cli-follow-2")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            appended = await api.inspect(entry["task_id"])
            assert appended["task_id"] == entry["task_id"]
            assert [item["request_id"] for item in appended["inputs"]] == [
                "cli-follow-1",
                "cli-follow-2",
            ]
            assert len(app.state.runtime.list_tasks()) == 1

            history = await cli.select(entry["task_id"])
            assert [item["content"] for item in history if item["role"] == "user"] == [
                "Create the shared development task",
                "Continue from the CLI",
                "Second CLI follow-up",
            ]

    asyncio.run(scenario())


def test_active_recovery_and_terminal_states_refuse_or_accept_explicitly(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            entry = await api.create_development(
                "Status conflict task", "web", "status-create-1"
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            task_id = entry["task_id"]

            slow = _SlowModel()
            app.state.execution.llm_factory = lambda: slow
            cli = RemoteConversation(api, mode="development")
            await cli.select(task_id)
            await cli.send("Hold this turn open", request_id="cli-hold-1")
            await asyncio.wait_for(slow.started.wait(), 15)
            running = await api.inspect(task_id)
            assert running["status"] == "running"

            with pytest.raises(httpx.HTTPStatusError) as busy:
                await api.message(
                    task_id,
                    "Must wait for the active turn",
                    source="web",
                    request_id="web-busy-1",
                    expected_sequence=running["sequence"],
                )
            assert busy.value.response.status_code == 409
            assert busy.value.response.json()["error"] == "task_busy"

            # The CLI translates the same conflict into an actionable message.
            with pytest.raises(ValueError, match="still executing"):
                await cli.send(
                    "CLI must translate the conflict", request_id="cli-busy-1"
                )

            # A detached client cursor cannot cancel the Server-owned execution.
            follower = asyncio.create_task(cli.wait())
            await asyncio.sleep(0.05)
            follower.cancel()
            with suppress(asyncio.CancelledError):
                await follower
            assert (await api.inspect(task_id))["status"] == "running"
            slow.release.set()
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            settled = await api.inspect(task_id)
            assert settled["status"] == "completed"
            assert settled["inputs"][-1]["request_id"] == "cli-hold-1"
            assert settled["inputs"][-1]["status"] == "completed"

            runtime = app.state.runtime
            recovery = runtime.inspect(task_id)
            runtime.record(
                recovery.model_copy(
                    update={
                        "status": "recovery_required",
                        "diagnostic": "test_recovery",
                    }
                ),
                "interrupted",
            )
            recovery_state = await api.inspect(task_id)
            with pytest.raises(httpx.HTTPStatusError) as blocked:
                await api.message(
                    task_id,
                    "Must be reviewed first",
                    source="cli",
                    request_id="cli-recovery-1",
                    expected_sequence=recovery_state["sequence"],
                )
            assert blocked.value.response.status_code == 409
            assert blocked.value.response.json()["error"] == "task_recovery_required"
            unchanged = await api.inspect(task_id)
            assert unchanged["status"] == "recovery_required"
            assert [item["request_id"] for item in unchanged["inputs"]] == [
                "cli-hold-1"
            ]

            failed = runtime.inspect(task_id)
            runtime.record(
                failed.model_copy(
                    update={"status": "failed", "diagnostic": "test_failed"}
                ),
                "failed",
            )
            failed_state = await api.inspect(task_id)
            with pytest.raises(httpx.HTTPStatusError) as terminal:
                await api.message(
                    task_id,
                    "Cannot continue a failed task",
                    source="web",
                    request_id="web-failed-1",
                    expected_sequence=failed_state["sequence"],
                )
            assert terminal.value.response.status_code == 409
            assert terminal.value.response.json()["error"] == "task_not_continuable"

            # A host-reviewed pause is a safe checkpoint for another input.
            paused = runtime.inspect(task_id)
            runtime.record(
                paused.model_copy(
                    update={"status": "paused", "result": "paused for the test"}
                ),
                "paused",
            )
            paused_state = await api.inspect(task_id)
            accepted = await api.message(
                task_id,
                "Continue after the safe pause",
                source="cli",
                request_id="cli-paused-1",
                expected_sequence=paused_state["sequence"],
            )
            assert accepted["status"] == "queued"
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            resumed = await api.inspect(task_id)
            assert resumed["status"] == "completed"
            assert resumed["task_id"] == task_id
            assert [item["request_id"] for item in resumed["inputs"]] == [
                "cli-hold-1",
                "cli-paused-1",
            ]
            assert resumed["inputs"][-1]["status"] == "completed"

    asyncio.run(scenario())


def test_uncertain_effect_settles_the_input_and_blocks_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            entry = await api.create_development(
                "Uncertain effect task", "web", "uncertain-create-1"
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            created = await api.inspect(entry["task_id"])

            async def uncertain(task: Any, emit: Any) -> str:
                raise TaskNeedsRecovery()

            app.state.runtime.executor = uncertain
            accepted = await api.message(
                entry["task_id"],
                "Touch the host without replaying it",
                source="cli",
                request_id="uncertain-1",
                expected_sequence=created["sequence"],
            )
            assert accepted["status"] == "queued"
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)

            interrupted = await api.inspect(entry["task_id"])
            assert interrupted["status"] == "recovery_required"
            assert interrupted["inputs"][-1]["request_id"] == "uncertain-1"
            assert interrupted["inputs"][-1]["status"] == "recovery_required"
            assert interrupted["inputs"][-1]["diagnostic"] == (
                "effects_require_host_review"
            )

            with pytest.raises(httpx.HTTPStatusError) as blocked:
                await api.message(
                    entry["task_id"],
                    "Must be reviewed first",
                    source="web",
                    request_id="uncertain-2",
                    expected_sequence=interrupted["sequence"],
                )
            assert blocked.value.response.status_code == 409
            assert blocked.value.response.json()["error"] == "task_recovery_required"

    asyncio.run(scenario())


def test_appended_input_evidence_is_redacted_and_reloads(tmp_path: Path) -> None:
    state = tmp_path / "state"
    forbidden = (tmp_path / "source", tmp_path / "worktrees")
    secret = "{{SECRET_1npazs7q}}"
    catalogue = RuntimeCatalogue(
        state, forbidden_roots=forbidden, sensitive_values=(secret,)
    )
    try:
        task = RuntimeTask(
            prompt="goal",
            source="cli",
            inputs=(
                TaskInput(
                    request_id="redacted-input",
                    source="cli",
                    result_excerpt=(secret * 200)[:INPUT_EXCERPT_LIMIT],
                    diagnostic=(secret * 50)[:INPUT_DIAGNOSTIC_LIMIT],
                ),
            ),
        )
        recorded = catalogue.record(task, "completed")
        excerpt = recorded.inputs[0].result_excerpt or ""
        diagnostic = recorded.inputs[0].diagnostic or ""
        assert secret not in excerpt and secret not in diagnostic
        assert "[redacted]" in excerpt
        assert len(excerpt) <= INPUT_EXCERPT_LIMIT
        assert len(diagnostic) <= INPUT_DIAGNOSTIC_LIMIT
    finally:
        catalogue.close()

    # A growing redaction replacement must not leave state that fails reload.
    reopened = RuntimeCatalogue(state, forbidden_roots=forbidden)
    try:
        assert reopened.data.tasks[task.task_id].inputs == recorded.inputs
    finally:
        reopened.close()
