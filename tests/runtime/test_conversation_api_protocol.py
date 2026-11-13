"""Protocol-level Conversation API safety tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from cairn.agent.budget import RunBudget
from cairn.agent.context.window import ContextBuilder
from cairn.agent.models.catalog import NamedProvider
from cairn.agent.models.executor import ModelExecutor
from cairn.agent.models.messages import LLMResponse, LLMUsage, Message
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.agent.models.runtime import ProviderRuntime
from cairn.agent.models.selection import ModelConfig, ModelManager, ProviderConfig
from cairn.conversation.models import ConversationSource, RunStatus
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


class ChatModel(OfflineDevelopmentModel):
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del tools
        return LLMResponse(
            content="reply", usage=LLMUsage(input_tokens=1, output_tokens=1)
        )


class SlowModel(OfflineDevelopmentModel):
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del messages, tools
        await self.release.wait()
        return LLMResponse(
            content="slow", usage=LLMUsage(input_tokens=1, output_tokens=1)
        )


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


def test_conversation_create_request_id_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            first = await api.create_conversation(
                "same", source="cli", request_id="conversation-request"
            )
            second = await api.create_conversation(
                "same", source="cli", request_id="conversation-request"
            )
            assert first["conversation_id"] == second["conversation_id"]
            try:
                await api.create_conversation(
                    "different",
                    source="cli",
                    request_id="conversation-request",
                )
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 409
            else:
                raise AssertionError("different create payload must be rejected")
            assert len(app.state.conversations.list_conversations()) == 1

    asyncio.run(scenario())


def test_cancel_is_post_only_and_get_never_mutates(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        slow = SlowModel()
        app.state.execution.llm_factory = lambda: slow
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("cancel", source="cli")
            run = await api.send_conversation(
                conversation["conversation_id"],
                "cancel",
                source="cli",
                request_id="cancel-request",
            )
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] == "running":
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "running"
            try:
                await api.request("GET", f"runs/{run['run_id']}/cancel")
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 405
            else:
                raise AssertionError("GET cancel must not be allowed")
            assert (await api.get_run(run["run_id"]))["status"] == "running"
            cancelled = await api.cancel_run(run["run_id"])
            assert cancelled["status"] == "cancelled"
            assert (await api.get_run(run["run_id"]))["status"] == "cancelled"

    asyncio.run(scenario())


def test_recovery_required_blocks_new_ordinary_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("recovery", source="cli")
            run = app.state.conversations.store.accept_run(
                conversation_id=conversation["conversation_id"],
                prompt="old",
                source=ConversationSource.CLI,
                request_id="old-request",
            )
            app.state.conversations.store.update_run(
                run.run_id,
                status=RunStatus.RECOVERY_REQUIRED,
                error="effects_require_host_review",
            )
            try:
                await api.send_conversation(
                    conversation["conversation_id"],
                    "new",
                    source="cli",
                    request_id="new-request",
                )
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 409
                assert (
                    error.response.json()["error"] == "conversation_recovery_required"
                )
            else:
                raise AssertionError("recovery must block a new ordinary Run")

    asyncio.run(scenario())


def test_workbench_admits_and_persists_only_approved_run_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        primary, alternate = "fixture/primary", "fixture/alternate"
        config = ProviderConfig(
            "https://fixture.invalid",
            "TEST_KEY",
            (ModelConfig("chat", (primary, alternate)),),
        )
        model_manager = ModelManager(config)
        used: list[str] = []

        def model_for(model_id: str) -> tuple[ChatModel, ContextBuilder]:
            used.append(model_id)
            return ChatModel(), ContextBuilder()

        app.state.execution.provider_runtime = ProviderRuntime(
            provider=NamedProvider("fixture", config),
            model_manager=model_manager,
            model_executor=ModelExecutor(model_manager, model_for),
            llm=ChatModel(),
            context_builder=ContextBuilder(),
            runtime_factory=model_for,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8777",
            ) as client,
        ):
            auth = {"Authorization": "Bearer zzzz"}
            prefix = "/api/runtime/"
            created = await client.post(
                prefix + "conversations",
                headers=auth,
                json={"prompt": "choose model", "source": "web"},
            )
            conversation_id = created.raise_for_status().json()["conversation_id"]
            path = prefix + f"conversations/{conversation_id}/messages"
            payload = {
                "prompt": "choose model",
                "source": "web",
                "request_id": "selected-model",
            }
            unknown = await client.post(
                path, headers=auth, json={**payload, "model_id": "outside/group"}
            )
            assert unknown.status_code == 409
            requested = {**payload, "model_id": alternate}
            admitted = await client.post(path, headers=auth, json=requested)
            run_id = admitted.raise_for_status().json()["run_id"]
            await asyncio.wait_for(app.state.conversations.queue.join(), timeout=5)

            result = (
                (await client.get(prefix + f"runs/{run_id}", headers=auth))
                .raise_for_status()
                .json()
            )
            assert result["status"] == "completed"
            assert result["model_id"] == alternate
            assert used and set(used) == {alternate}
            assert model_manager.current_model_id() == primary
            repeat = await client.post(path, headers=auth, json=requested)
            assert repeat.raise_for_status().json()["run_id"] == run_id
            changed = await client.post(
                path, headers=auth, json={**payload, "model_id": primary}
            )
            assert changed.status_code == 409

            rows = (
                (await client.get(prefix + "runs", headers=auth))
                .raise_for_status()
                .json()
            )
            assert [row["run_id"] for row in rows] == [run_id]
            assert (await client.get(prefix + "runs")).status_code == 401
            wrong_workspace = await client.get(
                prefix + "runs", headers={**auth, "X-Cairn-Workspace": "file:///wrong"}
            )
            assert wrong_workspace.status_code == 409

    asyncio.run(scenario())


def test_conversation_archive_hides_and_restores_listing(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            created = await api.create_conversation(
                "archivable", source="web", request_id="archive-create"
            )
            conversation_id = created["conversation_id"]
            archived = await api.request(
                "POST", f"conversations/{conversation_id}/archive"
            )
            assert archived["archived_at"]

            listed = await api.request("GET", "conversations")
            assert conversation_id not in {item["conversation_id"] for item in listed}
            with_archived = await api.request("GET", "conversations?include_archived=1")
            assert conversation_id in {
                item["conversation_id"] for item in with_archived
            }
            detail = await api.get_conversation(conversation_id)
            assert detail["conversation"]["archived_at"] == archived["archived_at"]

            restored = await api.request(
                "POST", f"conversations/{conversation_id}/unarchive"
            )
            assert restored["archived_at"] is None
            listed = await api.request("GET", "conversations")
            assert conversation_id in {item["conversation_id"] for item in listed}

    asyncio.run(scenario())


def test_conversation_rename_and_delete(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = _app(tmp_path)
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            created = await api.create_conversation(
                "rename me", source="web", request_id="rename-create"
            )
            conversation_id = created["conversation_id"]
            renamed = await api.request(
                "POST",
                f"conversations/{conversation_id}/rename",
                {"title": "  Operator   title  "},
            )
            assert renamed["title"] == "Operator title"
            detail = await api.get_conversation(conversation_id)
            assert detail["conversation"]["title"] == "Operator title"

            deleted = await api.request(
                "POST", f"conversations/{conversation_id}/delete"
            )
            assert deleted == {"deleted": conversation_id}
            listed = await api.request("GET", "conversations")
            assert conversation_id not in {item["conversation_id"] for item in listed}
            recreated = await api.create_conversation(
                "rename me", source="web", request_id="rename-create"
            )
            assert recreated["conversation_id"] != conversation_id

    asyncio.run(scenario())
