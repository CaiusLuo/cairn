"""Unified explicit Development entry through the Server service boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.conversation.models import ConversationSource, RunStatus
from cairn.development.models import HandoffStatus
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


class _NoChatModel(OfflineDevelopmentModel):
    """The explicit entry must not need a chat model call."""

    calls = 0

    async def generate(self, messages, tools=None) -> LLMResponse:  # type: ignore[no-untyped-def]
        type(self).calls += 1
        raise AssertionError("explicit Development entry must not call the chat model")


def _app(tmp_path: Path) -> Any:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    return runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="zzzz",
    )


def _client(app: Any) -> RuntimeClient:
    return RuntimeClient(
        "http://127.0.0.1:8777",
        "zzzz",
        transport=httpx.ASGITransport(app=app),
    )


def test_explicit_development_entry_creates_one_linked_identity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _NoChatModel.calls = 0
        app = _app(tmp_path)
        app.state.execution.llm_factory = _NoChatModel
        async with app.router.lifespan_context(app):
            api = _client(app)
            first = await api.request(
                "POST",
                "developments",
                {
                    "prompt": "Create the explicit development task",
                    "source": "cli",
                    "request_id": "dev-entry-1",
                },
            )
            assert first["task_id"]
            assert first["run_id"]
            assert first["conversation_id"]
            store = app.state.conversations.store
            run = store.get_run(first["run_id"])
            assert run is not None
            assert run.task_id == first["task_id"]
            assert run.conversation_id == first["conversation_id"]
            handoff = store.get_handoff(run.run_id)
            assert handoff is not None
            assert handoff.status is HandoffStatus.COMPLETED
            assert handoff.runtime_task_id == first["task_id"]
            assert handoff.domain_task_id == first["task_id"]
            assert len(store.list_tasks()) == 1
            listed = await api.request("GET", "tasks")
            assert any(task["task_id"] == first["task_id"] for task in listed)
            # The explicit entry itself does not call the chat model. The
            # development worker may invoke its own executor later; at this
            # point the request has already returned and created its identity.
            assert _NoChatModel.calls == 0

    asyncio.run(scenario())


def test_same_request_id_is_idempotent_and_different_ids_are_distinct(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            first = await api.request(
                "POST",
                "developments",
                {
                    "prompt": "Idempotent explicit goal",
                    "source": "web",
                    "request_id": "dev-same",
                },
            )
            second = await api.request(
                "POST",
                "developments",
                {
                    "prompt": "Idempotent explicit goal",
                    "source": "web",
                    "request_id": "dev-same",
                },
            )
            assert first["task_id"] == second["task_id"]
            assert first["run_id"] == second["run_id"]
            assert len(app.state.conversations.store.list_tasks()) == 1

            other = await api.request(
                "POST",
                "developments",
                {
                    "prompt": "A different explicit goal",
                    "source": "web",
                    "request_id": "dev-other",
                },
            )
            assert other["task_id"] != first["task_id"]
            assert len(app.state.conversations.store.list_tasks()) == 2

    asyncio.run(scenario())


def test_explicit_development_entry_rejects_reused_request_with_different_input(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            await api.request(
                "POST",
                "developments",
                {
                    "prompt": "Original goal",
                    "source": "web",
                    "request_id": "dev-conflict",
                },
            )
            with pytest.raises(httpx.HTTPStatusError) as conflict:
                await api.request(
                    "POST",
                    "developments",
                    {
                        "prompt": "Changed goal",
                        "source": "web",
                        "request_id": "dev-conflict",
                    },
                )
            assert conflict.value.response.status_code == 409

    asyncio.run(scenario())


def test_old_task_api_still_creates_and_queries_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            task = await api.request(
                "POST",
                "tasks",
                {
                    "prompt": "Legacy task entry",
                    "source": "cli",
                    "mode": "development",
                    "request_id": "legacy-task-1",
                },
            )
            assert task["task_id"]
            inspected = await api.request("GET", "tasks/" + task["task_id"])
            assert inspected["task_id"] == task["task_id"]

    asyncio.run(scenario())


def test_web_created_task_is_queryable_by_cli_and_cli_created_by_web(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            web_created = await api.create_development(
                "Create from Web", "web", "web-created-1"
            )
            inspected = await api.request("GET", "tasks/" + web_created["task_id"])
            assert inspected["task_id"] == web_created["task_id"]

            cli_created = await api.create_development(
                "Create from CLI", "cli", "cli-created-1"
            )
            all_tasks = await api.request("GET", "tasks")
            assert {web_created["task_id"], cli_created["task_id"]} <= {
                task["task_id"] for task in all_tasks
            }

    asyncio.run(scenario())


def test_explicit_development_preserves_conversation_history_origin(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            conversation = await api.create_conversation("History source")
            chat = await api.send_conversation(
                conversation["conversation_id"],
                "History source",
                source="cli",
                request_id="history-chat-1",
            )
            for _ in range(100):
                current = await api.get_run(chat["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current

            entry = await api.create_development(
                "Use the committed history",
                "cli",
                "history-development-1",
                conversation_id=conversation["conversation_id"],
            )
            handoff = app.state.conversations.store.get_handoff(entry["run_id"])
            assert handoff is not None
            assert handoff.history_origins
            assert handoff.status is HandoffStatus.COMPLETED

    asyncio.run(scenario())


def test_explicit_development_handoff_failure_retries_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            handoff = app.state.conversations.development_handoff
            original = handoff._ensure_runtime_task

            def fail_once(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("injected handoff failure")

            monkeypatch.setattr(handoff, "_ensure_runtime_task", fail_once)
            with pytest.raises(httpx.HTTPStatusError) as failed:
                await api.create_development(
                    "Recoverable explicit goal",
                    "cli",
                    "recoverable-dev",
                )
            assert failed.value.response.status_code == 503

            monkeypatch.setattr(handoff, "_ensure_runtime_task", original)
            recovered = await api.create_development(
                "Recoverable explicit goal",
                "cli",
                "recoverable-dev",
            )
            assert recovered["task_id"]
            assert len(app.state.conversations.store.list_tasks()) == 1
            handoff_record = app.state.conversations.store.get_handoff(
                recovered["run_id"]
            )
            assert handoff_record is not None
            assert handoff_record.status is HandoffStatus.COMPLETED

    asyncio.run(scenario())


class _FakeDevelopmentApi:
    def __init__(self, *, lose_first_response: bool = False) -> None:
        self.created: dict[str, dict[str, Any]] = {}
        self.request_ids: list[str] = []
        self.lose_first_response = lose_first_response
        self.failed_once = False

    async def create_development(
        self,
        prompt: str,
        source: str,
        request_id: str,
        *,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        del source, conversation_id
        self.request_ids.append(request_id)
        if request_id not in self.created:
            self.created[request_id] = {
                "task": {"task_id": "task-" + request_id, "prompt": prompt}
            }
            if self.lose_first_response and not self.failed_once:
                self.failed_once = True
                raise httpx.TransportError("response lost after acceptance")
        return self.created[request_id]

    async def inspect(self, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id}


def test_cli_development_retry_reuses_request_id_after_lost_response() -> None:
    async def scenario() -> None:
        from cairn.interfaces.cli.runtime_chat import RemoteDevelopment

        api = _FakeDevelopmentApi(lose_first_response=True)
        development = RemoteDevelopment(api)  # type: ignore[arg-type]
        with pytest.raises(httpx.TransportError):
            await development.start("lost response goal")
        assert development.pending is not None
        task = await development.start("lost response goal")
        assert task["task_id"] == "task-" + api.request_ids[0]
        assert len(set(api.request_ids)) == 1
        assert development.pending is None

    asyncio.run(scenario())


def test_cli_development_pending_blocks_a_new_request_id() -> None:
    async def scenario() -> None:
        from cairn.interfaces.cli.runtime_chat import RemoteDevelopment

        api = _FakeDevelopmentApi(lose_first_response=True)
        development = RemoteDevelopment(api)  # type: ignore[arg-type]
        with pytest.raises(httpx.TransportError):
            await development.start("original goal")
        with pytest.raises(ValueError, match="Retry"):
            await development.start("different goal")
        assert len(api.request_ids) == 1

    asyncio.run(scenario())


def test_recovery_required_conversation_rejects_new_development(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            store = app.state.conversations.store
            conversation = store.create_conversation(
                workspace=tmp_path / "source",
                source=ConversationSource.CLI,
                initial_prompt="recovery conversation",
            )
            store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.RECOVERY_REQUIRED,
                prompt="needs review",
                request_id="recovery-existing",
                source=ConversationSource.CLI,
            )
            with pytest.raises(httpx.HTTPStatusError) as rejection:
                await api.create_development(
                    "must not bypass recovery",
                    "web",
                    "recovery-new",
                    conversation_id=conversation.conversation_id,
                )
            assert rejection.value.response.status_code == 409
            assert not any(
                task["task_id"] == "recovery-new"
                for task in await api.request("GET", "tasks")
            )

    asyncio.run(scenario())


def test_same_request_can_retry_after_accepted_recovery_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            api = _client(app)
            entry = await api.create_development(
                "accepted before recovery",
                "cli",
                "accepted-recovery",
            )
            store = app.state.conversations.store
            store.update_run(
                entry["run_id"],
                status=RunStatus.RECOVERY_REQUIRED,
                error="test_recovery",
            )
            retry = await api.create_development(
                "accepted before recovery",
                "cli",
                "accepted-recovery",
            )
            assert retry["task_id"] == entry["task_id"]
            assert retry["run_id"] == entry["run_id"]

    asyncio.run(scenario())


def test_active_run_mutual_exclusion_still_applies_to_development(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            store = app.state.conversations.store
            conversation = store.create_conversation(
                workspace=tmp_path / "source",
                source=ConversationSource.CLI,
                initial_prompt="active conversation",
            )
            store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.RUNNING,
                prompt="active",
                request_id="active-existing",
                source=ConversationSource.CLI,
            )
            api = _client(app)
            with pytest.raises(httpx.HTTPStatusError) as rejection:
                await api.create_development(
                    "must wait for active run",
                    "web",
                    "active-new",
                    conversation_id=conversation.conversation_id,
                )
            assert rejection.value.response.status_code == 409
            assert len(app.state.conversations.store.list_tasks()) == 0

    asyncio.run(scenario())


def test_needs_review_handoff_cannot_be_silently_retried(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        async with app.router.lifespan_context(app):
            store = app.state.conversations.store
            conversation = store.create_conversation(
                workspace=tmp_path / "source",
                source=ConversationSource.CLI,
                initial_prompt="needs review",
            )
            run = store.accept_run(
                conversation_id=conversation.conversation_id,
                prompt="needs review",
                source=ConversationSource.CLI,
                request_id="development:needs-review",
                mode="development",
                status=RunStatus.RUNNING,
            )
            store.begin_handoff(
                run_id=run.run_id,
                conversation_id=conversation.conversation_id,
                request_id="handoff:" + run.run_id,
                prompt="needs review",
                source="cli",
            )
            store.update_handoff(
                run.run_id,
                status=HandoffStatus.NEEDS_REVIEW,
                diagnostic="operator_review_required",
            )
            api = _client(app)
            with pytest.raises(httpx.HTTPStatusError) as rejection:
                await api.create_development(
                    "needs review",
                    "cli",
                    "needs-review",
                    conversation_id=conversation.conversation_id,
                )
            assert rejection.value.response.status_code == 409
            handoff = store.get_handoff(run.run_id)
            assert handoff is not None
            assert handoff.status is HandoffStatus.NEEDS_REVIEW
            assert handoff.diagnostic == "operator_review_required"
            current = store.get_run(run.run_id)
            assert current is not None
            assert current.status is RunStatus.RECOVERY_REQUIRED

    asyncio.run(scenario())
