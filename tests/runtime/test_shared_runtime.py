import asyncio
import subprocess
from pathlib import Path
from typing import Any

import httpx

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, Message
from cairn.infra.runtime_client import RuntimeClient
from cairn.runtime.offline_model import OfflineDevelopmentModel
from cairn.server import runtime_app
from cairn.terminal.wechat.runtime import WechatRuntimeAdapter


def repository(root: Path) -> Path:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("shared runtime test\n")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    return root


def test_cli_web_wechat_share_real_execution_and_worktree(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = repository(tmp_path / "source")
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-runtime-token",
        )
        started, release = asyncio.Event(), asyncio.Event()

        class SlowModel(OfflineDevelopmentModel):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                started.set()
                await release.wait()
                return await super().generate(messages, tools)

        app.state.execution.llm_factory = SlowModel
        transport = httpx.ASGITransport(app=app)
        api = RuntimeClient(
            "http://127.0.0.1:8777", "test-runtime-token", transport=transport
        )
        wechat = WechatRuntimeAdapter(api)
        async with app.router.lifespan_context(app):
            first = await api.submit(
                "Create the acceptance result", "cli", "cli-message-1"
            )
            await asyncio.wait_for(started.wait(), 15)
            progress = await api.inspect(first["task_id"])
            assert progress["status"] == "running" and progress["worktree"]
            reply = await asyncio.wait_for(
                wechat.reply("/status " + first["task_id"]), 0.5
            )
            assert first["task_id"] in reply and progress["worktree"] in reply
            again = await api.submit(
                "Create the acceptance result", "cli", "cli-message-1"
            )
            assert again["task_id"] == first["task_id"]
            release.set()
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            final = await api.inspect(first["task_id"])
            assert final["status"] == "completed", final
            artifact = Path(final["worktree"]) / "runtime-result.txt"
            assert artifact.read_text() == "Cairn shared runtime task completed\n"
            assert not (source / "runtime-result.txt").exists()
            events = await api.request("GET", f"tasks/{first['task_id']}/events")
            assert any(event["kind"] == "tool_result" for event in events["events"])
            history = await api.request("GET", f"tasks/{first['task_id']}/history")
            assert any(message["role"] == "tool" for message in history)
            assert "已提交历史" in await wechat.reply("/history " + first["task_id"])
            second = await api.submit(
                "Create another acceptance result", "web", "web-message-1"
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            assert (await api.inspect(second["task_id"]))["status"] == "completed"
            third = await wechat.reply(
                "/task Create from WeChat", message_id="wechat-message-1"
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            assert "task-" in third
            tasks = await api.request("GET", "tasks")
            assert {task["source"] for task in tasks} == {"cli", "web", "wechat"}
            assert len(tasks) == 3
            # Existing Web snapshot reads the same projected task IDs.
            for _ in range(100):
                if not app.state.runtime.projections:
                    break
                await asyncio.sleep(0.02)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8777",
                headers={"Authorization": "Bearer test-runtime-token"},
            ) as web:
                snapshot_response = await web.get("/api/snapshot")
                assert snapshot_response.status_code == 200
                assert first["task_id"] in snapshot_response.text

    asyncio.run(scenario())


def test_safe_continue_uses_same_id_history_and_worktree(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = repository(tmp_path / "source")
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(1),
            token="test-token",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            task = await api.submit("Create an artifact", "cli")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            paused = await api.inspect(task["task_id"])
            assert paused["status"] == "paused", paused
            assert "最近工具结果" in paused["result"]
            before = Path(paused["worktree"]) / "runtime-result.txt"
            identity = before.stat().st_ino
            await api.control(task["task_id"], "continue")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            final = await api.inspect(task["task_id"])
            assert final["status"] == "completed", final
            assert final["worktree"] == paused["worktree"]
            assert final["session_id"] == paused["session_id"]
            assert (
                before.stat().st_ino == identity
            )  # write_file replaces inode if repeated.
            events = await api.request("GET", f"tasks/{task['task_id']}/events")
            assert sum(event["kind"] == "tool_call" for event in events["events"]) == 1

    asyncio.run(scenario())


def test_runtime_http_requires_authentication_and_rejects_authority_arguments(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = runtime_app(
            repository(tmp_path / "source"),
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8777"
            ) as client,
        ):
            assert (
                await client.post(
                    "/api/runtime/tasks", json={"prompt": "run", "source": "web"}
                )
            ).status_code == 401
            client.headers["Authorization"] = "Bearer test-token"
            assert (
                await client.post(
                    "/api/runtime/tasks",
                    json={"prompt": "run", "source": "web", "allow_shell": True},
                )
            ).status_code == 409
            assert (
                await client.post(
                    "/api/runtime/tasks",
                    headers={"Origin": "https://foreign.invalid"},
                    json={"prompt": "run", "source": "web"},
                )
            ).status_code == 403
            assert app.state.runtime.list_tasks() == []

    asyncio.run(scenario())


def test_wechat_service_forwards_after_sender_admission(tmp_path: Path) -> None:
    import json
    from dataclasses import replace

    from cairn.infra.wechat.state import WechatState
    from cairn.terminal.wechat.serve import ServeReport, _Inbound, _Service
    from tests.support.fake_ilink import FakeILink
    from tests.support.wechat_service import _client, _config

    async def scenario() -> None:
        source = repository(tmp_path / "source")
        app = runtime_app(
            source,
            tmp_path / "runtime",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )
        token = tmp_path / "runtime.token"
        token.write_text("test-token")
        token.chmod(0o600)

        async def no_legacy_execution(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Wechat must not start its own Agent")

        async with app.router.lifespan_context(app):
            first = await api.submit("Create from CLI", "cli")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            with FakeILink() as fake:
                config = replace(
                    _config(fake, source, tmp_path / "wechat"),
                    runtime_url="http://127.0.0.1:8777",
                    runtime_token_file=token,
                )
                state = WechatState(config.state_dir)
                bridge = _Service(
                    config=config,
                    state=state,
                    client=_client(fake),
                    executor=no_legacy_execution,
                    report=ServeReport(),
                    stop_event=None,
                )
                bridge._runtime_adapter = WechatRuntimeAdapter(api)
                inbound = _Inbound(
                    key="msg:status",
                    sender="u1",
                    context_token="ctx",
                    text="/status " + first["task_id"],
                    stable=True,
                )
                await bridge._handle(inbound)
                await asyncio.gather(*tuple(bridge._intent_tasks))
                assert first["task_id"] in json.dumps(fake.sent)
                assert len(app.state.runtime.list_tasks()) == 1
                stranger = _Inbound(
                    key="msg:stranger",
                    sender="stranger",
                    context_token="ctx",
                    text="Create a task",
                    stable=True,
                )
                await bridge._handle(stranger)
                assert not bridge._intent_tasks
                assert len(app.state.runtime.list_tasks()) == 1

    asyncio.run(scenario())


def test_deadline_requires_review_and_never_replays_write(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import pytest

    from cairn.infra.tools.write import WriteTool

    async def scenario() -> None:
        source = repository(tmp_path / "source")
        original = WriteTool.execute
        calls = 0

        async def interrupted_write(
            self: WriteTool, arguments: dict[str, Any], **kwargs: Any
        ) -> Any:
            nonlocal calls
            calls += 1
            await original(self, arguments, **kwargs)
            await asyncio.Event().wait()

        monkeypatch.setattr(WriteTool, "execute", interrupted_write)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5, max_seconds=0.15),
            token="test-token",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            task = await api.submit("Create artifact", "web")
            ident = task["task_id"]
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            interrupted = await api.inspect(ident)
            assert interrupted["status"] == "recovery_required", interrupted
            artifact = Path(interrupted["worktree"]) / "runtime-result.txt"
            inode = artifact.stat().st_ino
            with pytest.raises(httpx.HTTPStatusError):
                await api.control(ident, "continue")
            report = await api.request("GET", f"tasks/{ident}/recovery")
            payload = {
                key: report[key] for key in ("expected_sequence", "revision", "action")
            }
            payload["note"] = (
                "Artifact already exists and was verified; keep it and summarize only."
            )
            payload["reviewed_effects"] = False
            with pytest.raises(httpx.HTTPStatusError):
                await api.request("POST", f"tasks/{ident}/reconcile", payload)
            payload["reviewed_effects"] = True
            stale = {**payload, "expected_sequence": payload["expected_sequence"] - 1}
            with pytest.raises(httpx.HTTPStatusError):
                await api.request("POST", f"tasks/{ident}/reconcile", stale)
            acknowledged = await api.request(
                "POST", f"tasks/{ident}/reconcile", payload
            )
            assert acknowledged["status"] == "paused" and calls == 1
            await api.control(ident, "continue")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            final = await api.inspect(ident)
            assert final["status"] == "completed", final
            assert final["session_id"] == interrupted["session_id"]
            assert artifact.stat().st_ino == inode and calls == 1

    asyncio.run(scenario())


def test_pause_survives_service_restart_with_usage_and_original_worktree(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = repository(tmp_path / "source")

        def build() -> Any:
            return runtime_app(
                source,
                tmp_path / "state",
                tmp_path / "worktrees",
                budget=RunBudget(1),
                token="test-token",
            )

        app = build()
        async with app.router.lifespan_context(app):
            task = app.state.runtime.submit("Create", "wechat")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            first = app.state.runtime.inspect(task.task_id)
            assert first.status == "paused"
            inode = (Path(first.worktree or "") / "runtime-result.txt").stat().st_ino
        reopened = build()
        async with reopened.router.lifespan_context(reopened):
            reopened.state.runtime.continue_task(task.task_id)
            await asyncio.wait_for(reopened.state.runtime.queue.join(), 30)
            final = reopened.state.runtime.inspect(task.task_id)
            assert final.status == "completed"
            assert (final.task_id, final.worktree, final.session_id) == (
                first.task_id,
                first.worktree,
                first.session_id,
            )
            assert (
                Path(final.worktree or "") / "runtime-result.txt"
            ).stat().st_ino == inode
            assert final.metrics["steps"] == 2 and final.metrics["tool_attempts"] == 1
            assert final.metrics["total_tokens"] == 60

    asyncio.run(scenario())


def test_agent_cannot_read_runtime_private_state(tmp_path: Path) -> None:
    from cairn.core.models import ToolCall
    from cairn.infra.credential_paths import is_credential_path

    async def scenario() -> None:
        source = repository(tmp_path / "source")
        state = tmp_path / "state"
        app = runtime_app(
            source,
            state,
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="private-runtime-value",
        )
        private = state / "ordinary.txt"
        private.write_text("private-runtime-value")

        class ReadState(OfflineDevelopmentModel):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                if any(m.role == "tool" for m in messages):
                    return LLMResponse(content="Read denied")
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="read",
                            name="read_file",
                            arguments={"path": str(private)},
                        )
                    ]
                )

        app.state.execution.llm_factory = ReadState
        async with app.router.lifespan_context(app):
            import pytest

            with pytest.raises(ValueError):
                app.state.runtime.submit("Echo private-runtime-value", "web")
            task = app.state.runtime.submit("Read ordinary state file", "cli")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            final = app.state.runtime.inspect(task.task_id)
            history = app.state.execution.history(final)
            assert final.status == "completed"
            assert "private-runtime-value" not in str(history)
            tool = next(m for m in history if m["role"] == "tool")
            assert (
                "permission" in str(tool).lower() or "credential" in str(tool).lower()
            )
            assert not is_credential_path(private)  # Context does not leak to the host.

    asyncio.run(scenario())


def test_unknown_tool_effect_stops_before_another_model_request(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from cairn.infra.tools.write import WriteTool

    async def scenario() -> None:
        source = repository(tmp_path / "source")
        calls = 0
        original = WriteTool.execute

        async def broken_write(
            self: WriteTool, arguments: dict[str, Any], **kwargs: Any
        ) -> Any:
            await original(self, arguments, **kwargs)
            raise RuntimeError("unknown after write")

        class CountModel(OfflineDevelopmentModel):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                nonlocal calls
                calls += 1
                return await super().generate(messages, tools)

        monkeypatch.setattr(WriteTool, "execute", broken_write)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        app.state.execution.llm_factory = CountModel
        async with app.router.lifespan_context(app):
            task = app.state.runtime.submit("Create", "cli")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            final = app.state.runtime.inspect(task.task_id)
            assert final.status == "recovery_required", final
            assert calls == 1
            assert (Path(final.worktree or "") / "runtime-result.txt").is_file()

    asyncio.run(scenario())


def test_websocket_stream_is_transient_and_reconnects_from_cursor(
    tmp_path: Path,
) -> None:
    from starlette.testclient import TestClient

    from cairn.core.llm import LLMStreamChunk
    from cairn.core.models import LLMUsage
    from tests.support.streaming import ScriptedStreamLLM, StreamBarrier

    app = runtime_app(
        repository(tmp_path / "source"),
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="test-runtime-token",
    )
    entered, release = asyncio.Event(), asyncio.Event()
    model = ScriptedStreamLLM(
        [
            StreamBarrier(entered, release),
            LLMStreamChunk(text="Live preview"),
            LLMStreamChunk(
                finish_reason="stop", usage=LLMUsage(input_tokens=20, output_tokens=10)
            ),
        ]
    )
    app.state.execution.llm_factory = lambda: model
    headers = {"Authorization": "Bearer test-runtime-token", "host": "127.0.0.1:8777"}
    with TestClient(app, base_url="http://127.0.0.1:8777", headers=headers) as client:
        task = client.post(
            "/api/runtime/tasks", json={"prompt": "Summarize", "source": "web"}
        ).json()
        ident = task["task_id"]
        assert client.portal is not None
        client.portal.call(asyncio.wait_for, entered.wait(), 10)
        with client.websocket_connect(
            f"/api/runtime/tasks/{ident}/stream", headers=headers
        ) as socket:
            events = []
            while (message := socket.receive_json())["type"] != "ready":
                events.append(message["event"])
            cursor = message["cursor"]
            client.portal.call(release.set)
            previews = []
            while (message := socket.receive_json())["type"] != "settled":
                if message["type"] == "preview":
                    previews.append(message["text"])
            assert "".join(previews) == "Live preview"
        with client.websocket_connect(
            f"/api/runtime/tasks/{ident}/stream?after={cursor}", headers=headers
        ) as socket:
            replay = []
            while (message := socket.receive_json())["type"] != "ready":
                assert message["type"] == "event"
                replay.append(message["event"])
            assert all(event["sequence"] > cursor for event in replay)
        record = client.get(f"/api/runtime/tasks/{ident}").json()
        assert record["status"] == "completed"
        history = client.get(f"/api/runtime/tasks/{ident}/history").json()
        assert history[-1]["content"] == "Live preview"
        durable = app.state.runtime.catalogue.data.events[ident]
        assert all(event.kind != "model_text_delta" for event in durable)
        assert app.state.runtime.subscribers == {}


def test_approved_provider_runtime_is_reused_without_api_calls(tmp_path: Path) -> None:
    from cairn.core.context import ContextBudget, ContextBuilder
    from cairn.core.model_manager import ModelConfig, ModelManager, ProviderConfig
    from cairn.core.provider_catalog import NamedProvider
    from cairn.runtime.model_executor import ModelExecutor
    from cairn.runtime.provider_runtime import ProviderRuntime

    async def scenario() -> None:
        source = repository(tmp_path / "source")
        llm = OfflineDevelopmentModel()
        builder = ContextBuilder(budget=ContextBudget())
        provider = NamedProvider(
            "approved-test",
            ProviderConfig(
                "http://127.0.0.1:1/v1",
                "TEST_ONLY_KEY",
                (ModelConfig("dev", ("offline/model",)),),
            ),
        )
        manager = ModelManager(provider.config)

        def factory(model_id: str) -> tuple[Any, ContextBuilder]:
            return llm, builder

        executor = ModelExecutor(manager, factory)
        profile = ProviderRuntime(provider, manager, executor, llm, builder, factory)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
            provider_runtime=profile,
            secret_env_keys=frozenset({"TEST_ONLY_KEY"}),
        )
        async with app.router.lifespan_context(app):
            task = app.state.runtime.submit("Create", "cli")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            final = app.state.runtime.inspect(task.task_id)
            assert final.status == "completed", final
            report = app.state.execution.store.inspect(final.session_id)
            assert report.binding.provider.provider == "approved-test"
            assert report.binding.provider.model_ids == ("offline/model",)
            assert (Path(final.worktree or "") / "runtime-result.txt").is_file()

    asyncio.run(scenario())


def test_full_wechat_bridge_startup_does_not_initialize_legacy_agent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from dataclasses import replace

    import cairn.infra.runtime_client as client_module
    import cairn.terminal.wechat.serve as bridge_module
    from tests.support.fake_ilink import FakeILink
    from tests.support.wechat_service import _client, _config, _msg

    async def scenario() -> None:
        source = repository(tmp_path / "source")
        app = runtime_app(
            source,
            tmp_path / "runtime",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )
        token = tmp_path / "runtime.token"
        token.write_text("test-token")
        token.chmod(0o600)

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "Runtime bridge initialized a legacy execution dependency"
            )

        for name in (
            "default_task_executor",
            "default_conversation_executor",
            "default_conversation_classifier",
            "ScheduleStore",
        ):
            monkeypatch.setattr(bridge_module, name, forbidden)
        monkeypatch.setattr(client_module, "RuntimeClient", lambda *args, **kwargs: api)
        async with app.router.lifespan_context(app):
            with FakeILink() as fake:
                fake.updates.append(
                    {"ret": 0, "msgs": [_msg("/task Create through the full bridge")]}
                )
                config = replace(
                    _config(fake, source, tmp_path / "wechat", max_polls=None),
                    runtime_url="http://127.0.0.1:8777",
                    runtime_token_file=token,
                    schedule_store=tmp_path / "legacy-schedule",
                )
                from cairn.infra.wechat.state import WechatState

                WechatState(tmp_path / "runtime" / "wechat").save_account(
                    config.account
                )
                app.state.controls.wechat.restore_senders(("u1",))
                app.state.controls._save(trusted_senders=("u1",))
                stop = asyncio.Event()
                service = asyncio.create_task(
                    bridge_module.serve_wechat(
                        config=config,
                        client=_client(fake),
                        stop_event=stop,
                        allow_insecure_local_http=True,
                    )
                )
                try:
                    async with asyncio.timeout(15):
                        while not app.state.runtime.list_tasks():
                            if service.done():
                                service.result()
                                raise AssertionError(
                                    "Bridge stopped without submitting a task"
                                )
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(app.state.runtime.queue.join(), 30)
                    task = app.state.runtime.list_tasks()[0]
                    assert task.source == "wechat" and task.status == "completed"
                    assert (Path(task.worktree or "") / "runtime-result.txt").is_file()
                finally:
                    stop.set()
                    await asyncio.wait_for(service, 5)
                assert not (tmp_path / "legacy-schedule").exists()
                assert not (tmp_path / "wechat" / "sessions").exists()

    asyncio.run(scenario())


def test_cli_web_cli_continue_the_same_task_and_committed_history(
    tmp_path: Path,
) -> None:
    from cairn.terminal.runtime_chat import RemoteConversation

    async def scenario() -> None:
        app = runtime_app(
            repository(tmp_path / "source"),
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            cli = RemoteConversation(api, mode="development")
            accepted = await cli.send("Create artifact", request_id="cli-first")
            first = await asyncio.wait_for(cli.wait(), 30)
            assert first["status"] == "completed"
            artifact = Path(first["worktree"]) / "runtime-result.txt"
            inode = artifact.stat().st_ino
            await api.message(
                accepted["task_id"],
                "Inspect from Web",
                source="web",
                request_id="web-followup",
                expected_sequence=first["sequence"],
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            resumed = RemoteConversation(api)
            history = await resumed.select(accepted["task_id"])
            assert [m["content"] for m in history if m["role"] == "user"] == [
                "Create artifact",
                "Inspect from Web",
            ]
            await resumed.send("Continue from CLI", request_id="cli-followup")
            final = await asyncio.wait_for(resumed.wait(), 30)
            assert (final["task_id"], final["session_id"], final["worktree"]) == (
                first["task_id"],
                first["session_id"],
                first["worktree"],
            )
            history = await resumed.select(accepted["task_id"])
            assert [m["content"] for m in history if m["role"] == "user"] == [
                "Create artifact",
                "Inspect from Web",
                "Continue from CLI",
            ]
            assert artifact.stat().st_ino == inode
            # A lost receipt cannot duplicate an accepted message, even with its old cursor.
            replay = await api.message(
                first["task_id"],
                "Continue from CLI",
                source="cli",
                request_id="cli-followup",
                expected_sequence=first["sequence"],
            )
            assert replay["status"] == "completed" and replay["attempts"] == 3
            import pytest

            with pytest.raises(httpx.HTTPStatusError):
                await api.message(
                    first["task_id"],
                    "Stale writer",
                    source="web",
                    request_id="stale",
                    expected_sequence=first["sequence"],
                )
            assert len(app.state.runtime.list_tasks()) == 1

    asyncio.run(scenario())


def test_interactive_input_can_return_after_a_web_turn(
    tmp_path: Path, capsys: Any
) -> None:
    from cairn.terminal.input import CliInput
    from cairn.terminal.runtime_chat import main as shared_main

    async def scenario() -> None:
        app = runtime_app(
            repository(tmp_path / "source"),
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )

        class Input(CliInput):
            def __init__(self) -> None:
                self.index = 0

            async def read(self) -> str:
                self.index += 1
                if self.index == 1:
                    return "Create artifact"
                task = app.state.runtime.list_tasks()[0]
                if self.index == 2:
                    await api.message(
                        task.task_id,
                        "Web followup",
                        source="web",
                        request_id="web-input",
                        expected_sequence=task.sequence,
                    )
                    await asyncio.wait_for(app.state.runtime.queue.join(), 30)
                    return "/use " + str(task.task_id)
                return "CLI followup" if self.index == 3 else "/exit"

        async with app.router.lifespan_context(app):
            await shared_main(api, cli_input=Input())
            task = app.state.runtime.list_tasks()[0]
            history = app.state.execution.history(task)
            assert [m["content"] for m in history if m["role"] == "user"] == [
                "Create artifact",
                "Web followup",
                "CLI followup",
            ]
            assert task.attempts == 3
            assert "共享 Server" in capsys.readouterr().out

    asyncio.run(scenario())


def test_plain_chat_shares_history_without_git_worktree_or_repository_context(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from cairn.core.models import LLMUsage
    from cairn.infra.git.context import RepoContextProvider
    from cairn.terminal.runtime_chat import RemoteConversation

    async def scenario() -> None:
        source = repository(tmp_path / "source")
        seen: list[list[Message]] = []

        class ChatModel:
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                seen.append(messages)
                names = {item["function"]["name"] for item in tools or ()}
                assert {
                    "read_file",
                    "glob",
                    "grep",
                    "begin_development",
                    "memory_search",
                    "memory_remember",
                    "schedule_create",
                    "read_skill",
                } <= names
                assert "bash" not in names and "write_file" not in names
                assert not any(
                    "Runtime repository context" in (m.content or "") for m in messages
                )
                return LLMResponse(
                    content="Shared chat reply",
                    usage=LLMUsage(input_tokens=20, output_tokens=10),
                )

        async def no_scan(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "Ordinary conversation must not scan repository context"
            )

        monkeypatch.setattr(RepoContextProvider, "inspect", no_scan)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            cli = RemoteConversation(api)
            first = await cli.send("Hello")
            first = await asyncio.wait_for(cli.wait(), 20)
            await api.message(
                first["task_id"],
                "A Web followup",
                source="web",
                request_id="web-chat",
                expected_sequence=first["sequence"],
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 20)
            await cli.select(first["task_id"])
            await cli.send("Back to CLI")
            final = await asyncio.wait_for(cli.wait(), 20)
            assert (
                final["worktree"] is None and final["session_id"] == first["session_id"]
            )
            assert not (tmp_path / "worktrees").exists() or not list(
                (tmp_path / "worktrees").iterdir()
            )
            assert [
                m.content
                for m in seen[-1]
                if m.role == "user"
                and not (m.content or "").startswith(
                    "The following personal_agent_memory"
                )
            ] == ["Hello", "A Web followup", "Back to CLI"]
            assert app.state.runtime.queue.empty()
            assert not (source / "runtime-result.txt").exists()

    asyncio.run(scenario())


def test_agent_requests_development_with_checked_history_and_same_task_id(
    tmp_path: Path,
) -> None:
    from cairn.core.models import LLMUsage, ToolCall
    from cairn.terminal.runtime_chat import RemoteConversation

    async def scenario() -> None:
        source = repository(tmp_path / "source")
        development_context: list[Message] = []

        class AdaptiveModel(OfflineDevelopmentModel):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                names = {t["function"]["name"] for t in tools or ()}
                if "begin_development" in names:
                    user = [m.content for m in messages if m.role == "user"][-1]
                    if user == "Create the artifact now":
                        return LLMResponse(
                            tool_calls=[
                                ToolCall(
                                    id="begin",
                                    name="begin_development",
                                    arguments={"prompt": "Create the artifact now"},
                                )
                            ],
                            usage=LLMUsage(input_tokens=20, output_tokens=10),
                        )
                    return LLMResponse(
                        content="I remember conversation-marker-42",
                        usage=LLMUsage(input_tokens=20, output_tokens=10),
                    )
                development_context.extend(messages)
                return await super().generate(messages, tools)

        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        app.state.execution.llm_factory = AdaptiveModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            cli = RemoteConversation(api)
            await cli.send("conversation-marker-42")
            first = await asyncio.wait_for(cli.wait(), 30)
            assert first["worktree"] is None
            await cli.send("Create the artifact now")
            final = await asyncio.wait_for(cli.wait(), 30)
            assert final["status"] == "completed", final
            assert final["task_id"] == first["task_id"]
            assert (
                final["session_id"] != first["session_id"]
            )  # New workspace requires a fresh binding.
            assert len(final["history_origins"]) == 1
            assert (Path(final["worktree"]) / "runtime-result.txt").is_file()
            assert not (source / "runtime-result.txt").exists()
            assert any(
                "conversation-marker-42" in (m.content or "")
                for m in development_context
            )
            history = await cli.select(final["task_id"])
            assert any(m["content"] == "conversation-marker-42" for m in history)
            assert (
                sum(
                    m["role"] == "tool" and m["tool_call_id"] == "offline-write"
                    for m in history
                )
                == 1
            )
            assert final["metrics"]["window"]["steps"] == 3
            before = Path(final["worktree"]) / "runtime-result.txt"
            inode = before.stat().st_ino
            await cli.send("Report the result")
            next_turn = await asyncio.wait_for(cli.wait(), 30)
            assert (
                next_turn["worktree"] == final["worktree"]
                and before.stat().st_ino == inode
            )

        reopened = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        reopened_api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=reopened),
        )
        async with reopened.router.lifespan_context(reopened):
            cli = RemoteConversation(reopened_api)
            history = await cli.select(final["task_id"])
            assert any(m["content"] == "conversation-marker-42" for m in history)
            await cli.send("Continue after server restart")
            restored = await asyncio.wait_for(cli.wait(), 30)
            assert restored["status"] == "completed"
            assert (
                restored["session_id"] == final["session_id"]
                and before.stat().st_ino == inode
            )
            task = reopened.state.runtime.inspect(final["task_id"])
            bad_origin = task.history_origins[0].model_copy(update={"sha256": "0" * 64})
            reopened.state.runtime.record(
                task.model_copy(update={"history_origins": (bad_origin,)}),
                "corrupt_history_fixture",
            )
            # History provenance is checked before a subsequent model/tool turn.
            current = await reopened_api.inspect(final["task_id"])
            await reopened_api.message(
                final["task_id"],
                "Cannot use altered history",
                source="cli",
                request_id="tampered-history",
                expected_sequence=current["sequence"],
            )
            await asyncio.wait_for(reopened.state.runtime.queue.join(), 30)
            rejected = await reopened_api.inspect(final["task_id"])
            assert rejected["status"] == "recovery_required"
            assert before.stat().st_ino == inode

    asyncio.run(scenario())


def test_chat_cannot_write_or_grant_shell_before_worktree_transition(
    tmp_path: Path,
) -> None:
    from cairn.core.models import ToolCall

    async def scenario() -> None:
        source = repository(tmp_path / "source")

        class NoAuthorityModel:
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                if any(m.role == "tool" for m in messages):
                    return LLMResponse(content="Refused")
                return LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="illegal-write",
                            name="write_file",
                            arguments={"path": "escape.txt", "content": "no"},
                        ),
                        ToolCall(
                            id="illegal-grant",
                            name="begin_development",
                            arguments={"prompt": "write", "allow_shell": True},
                        ),
                    ]
                )

        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
            allow_shell=True,
        )
        app.state.execution.llm_factory = NoAuthorityModel
        async with app.router.lifespan_context(app):
            task = app.state.runtime.submit("Try to modify", "web", mode="conversation")
            await asyncio.wait_for(app.state.runtime.queue.join(), 20)
            task = app.state.runtime.inspect(task.task_id)
            assert task.status == "completed" and task.worktree is None
            assert not (source / "escape.txt").exists()
            events = app.state.runtime.events(task.task_id)
            assert sum(e.kind == "tool_error" for e in events) == 2

    asyncio.run(scenario())


def test_wechat_plain_messages_reuse_the_same_server_conversation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = runtime_app(
            repository(tmp_path / "source"),
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )
        adapter = WechatRuntimeAdapter(api)
        async with app.router.lifespan_context(app):
            await adapter.reply(
                "Hello", message_id="wx-chat-1", conversation_key="wechat:owner"
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 20)
            first = app.state.runtime.list_tasks()[0]
            await adapter.reply(
                "Remember the previous reply?",
                message_id="wx-chat-2",
                conversation_key="wechat:owner",
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 20)
            final = app.state.runtime.list_tasks()[0]
            assert len(app.state.runtime.list_tasks()) == 1
            assert final.session_id == first.session_id and final.worktree is None
            assert final.attempts == 2
            await adapter.reply(
                "Remember the previous reply?",
                message_id="wx-chat-2",
                conversation_key="wechat:owner",
            )
            assert app.state.runtime.inspect(first.task_id).attempts == 2
            await adapter.reply(
                "Hello", message_id="wx-chat-other", conversation_key="wechat:another"
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 20)
            assert len(app.state.runtime.list_tasks()) == 2

    asyncio.run(scenario())


def test_automatic_handoff_cannot_reset_the_host_budget(tmp_path: Path) -> None:
    from cairn.core.models import LLMUsage, ToolCall

    async def scenario() -> None:
        calls = 0

        class TransitionModel(OfflineDevelopmentModel):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                nonlocal calls
                calls += 1
                if any(
                    t["function"]["name"] == "begin_development" for t in tools or ()
                ):
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="begin",
                                name="begin_development",
                                arguments={"prompt": "Create artifact"},
                            )
                        ],
                        usage=LLMUsage(input_tokens=20, output_tokens=10),
                    )
                return await super().generate(messages, tools)

        app = runtime_app(
            repository(tmp_path / "source"),
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(1),
            token="test-token",
        )
        app.state.execution.llm_factory = TransitionModel
        async with app.router.lifespan_context(app):
            task = app.state.runtime.submit("Modify", "web", mode="conversation")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            paused = app.state.runtime.inspect(task.task_id)
            assert paused.status == "paused" and paused.worktree is None and calls == 1
            assert paused.development_prompt == "Create artifact"
            app.state.runtime.continue_task(task.task_id)
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            development = app.state.runtime.inspect(task.task_id)
            assert development.status == "paused" and development.worktree is not None
            artifact = Path(development.worktree) / "runtime-result.txt"
            inode = artifact.stat().st_ino
            app.state.runtime.continue_task(task.task_id)
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            final = app.state.runtime.inspect(task.task_id)
            assert (
                final.status == "completed" and final.worktree == development.worktree
            )
            assert artifact.stat().st_ino == inode and calls == 3

    asyncio.run(scenario())


def test_cancelled_chat_requires_review_and_can_continue_without_worktree(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        calls = 0
        seen = []

        class ChatModel:
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                nonlocal calls
                calls += 1
                if calls == 1:
                    started.set()
                    await asyncio.Event().wait()
                seen.extend(messages)
                return LLMResponse(content="Recovered chat")

        app = runtime_app(
            repository(tmp_path / "source"),
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            task = await api.submit(
                "Remember recovery-anchor", "cli", mode="conversation"
            )
            await asyncio.wait_for(started.wait(), 15)
            stopped = await api.control(task["task_id"], "cancel")
            assert (
                stopped["status"] == "recovery_required" and stopped["worktree"] is None
            )
            report = await api.request("GET", f"tasks/{task['task_id']}/recovery")
            payload = {
                key: report[key] for key in ("expected_sequence", "revision", "action")
            }
            payload.update(
                reviewed_effects=True,
                note="No project effects; resume the original question",
            )
            await api.request("POST", f"tasks/{task['task_id']}/reconcile", payload)
            await api.control(task["task_id"], "continue")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            final = await api.inspect(task["task_id"])
            assert final["status"] == "completed" and final["worktree"] is None
            assert final["session_id"] == stopped["session_id"]
            assert any("recovery-anchor" in (m.content or "") for m in seen)

    asyncio.run(scenario())


def test_chat_accepts_a_non_git_workspace_without_creating_worktree(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = tmp_path / "plain-directory"
        source.mkdir()
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        async with app.router.lifespan_context(app):
            task = app.state.runtime.submit("Hello", "web", mode="conversation")
            await asyncio.wait_for(app.state.runtime.queue.join(), 20)
            final = app.state.runtime.inspect(task.task_id)
            assert final.status == "completed" and final.worktree is None
            assert not list(source.iterdir())

    asyncio.run(scenario())


def test_workspace_identity_is_authenticated_and_checked_before_admission(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = repository(tmp_path / "项目 with spaces")
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="test-token",
        )
        transport = httpx.ASGITransport(app=app)
        right = RuntimeClient(
            "http://127.0.0.1:8777", "test-token", transport=transport, workspace=source
        )
        wrong = RuntimeClient(
            "http://127.0.0.1:8777",
            "test-token",
            transport=transport,
            workspace=tmp_path,
        )
        unauthorized = RuntimeClient(
            "http://127.0.0.1:8777",
            "wrong-token",
            transport=transport,
            workspace=source,
        )
        import pytest

        async with app.router.lifespan_context(app):
            await right.validate_workspace()
            with pytest.raises(httpx.HTTPStatusError) as denied:
                await unauthorized.validate_workspace()
            assert denied.value.response.status_code == 401
            with pytest.raises(ValueError, match="does not match"):
                await wrong.validate_workspace()
            # Per-request identity also blocks a client after the server changes roots.
            with pytest.raises(httpx.HTTPStatusError) as mismatch:
                await wrong.submit("Must not execute", "cli")
            assert mismatch.value.response.status_code == 409
            assert not app.state.runtime.list_tasks()
            task = await right.submit("hello", "cli", mode="conversation")
            await asyncio.wait_for(app.state.runtime.queue.join(), 30)
            assert (await right.inspect(task["task_id"]))["status"] == "completed"

    asyncio.run(scenario())
