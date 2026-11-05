import asyncio
import subprocess
from pathlib import Path
from typing import Any

import httpx

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, Message
from cairn.infra.runtime_client import RuntimeClient
from cairn.runtime.offline_model import OfflineDevelopmentModel
from cairn.terminal.wechat.runtime import WechatRuntimeAdapter
from cairn.web.runtime_server import runtime_app


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
