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
            reply = await wechat.reply("/status " + first["task_id"])
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
