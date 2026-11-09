"""Public clients exercise the same Server-owned controls, without paid APIs."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.infra.wechat.ilink import ILinkClient
from cairn.infra.wechat.state import WechatAccount, WechatState
from cairn.runtime.scheduling.runner import current_time
from cairn.terminal.input import CliInput
from cairn.terminal.runtime_chat import main as shared_main
from cairn.terminal.server_commands import ServerCommands
from cairn.terminal.wechat.runtime import (
    WechatConversationAdapter,
    WechatRuntimeAdapter,
)
from tests.support.control_runtime import control_runtime
from tests.support.fake_ilink import FakeILink
from tests.support.wechat_service import _msg


async def command(api: Any, name: str, args: str = "", **extra: Any) -> Any:
    from uuid import uuid4

    return await api.request(
        "POST",
        "commands",
        {
            "command": name,
            "args": args,
            "request_id": uuid4().hex,
            "source": "cli",
            **extra,
        },
    )


def save_account(root: Path, url: str = "https://example.invalid") -> None:
    WechatState(root / "state" / "wechat").save_account(
        WechatAccount("private-bot-token", "bot-1", url, "owner")
    )


def test_cli_commands_web_memory_and_restart_share_state(
    tmp_path: Path, capsys: Any
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        async with app.router.lifespan_context(app):
            lines = iter(
                [
                    "/wx status",
                    "/memory note 请用中文简短回复",
                    "/schedule list",
                    "/help",
                    "/exit",
                ]
            )

            class Input(CliInput):
                async def read(self) -> str:
                    return next(lines)

            await shared_main(api, cli_input=Input())
            assert (
                not app.state.runtime.list_tasks()
            )  # Commands never become model prompts.
            status = await api.request("GET", "management?section=memory")
            assert status["memory"]["daily_days"] == 1
            assert "请用中文简短回复" in app.state.controls.memory.render_context()
            assert app.state.controls.memory.store.root.is_relative_to(tmp_path)
        app, api = control_runtime(tmp_path)
        async with app.router.lifespan_context(app):
            assert (await api.request("GET", "management?section=memory"))["memory"][
                "daily_days"
            ] == 1
        output = capsys.readouterr().out
        assert "Shared commands:" not in output
        assert "trace_id" not in output

    asyncio.run(scenario())


def test_control_requests_are_idempotent_and_interruptions_are_not_replayed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        request = {
            "command": "/memory",
            "args": "note once-only",
            "request_id": "same-input",
            "source": "web",
        }
        async with app.router.lifespan_context(app):
            results = await asyncio.gather(
                *(api.request("POST", "commands", request) for _ in range(4))
            )
            assert all(value == results[0] for value in results)
            body = app.state.controls.memory.store.recent_daily_entries(current_time())[
                0
            ][1]
            assert body.count("once-only") == 1
            with pytest.raises(httpx.HTTPStatusError):
                await api.request(
                    "POST", "commands", {**request, "args": "note changed"}
                )
            app.state.controls._save(
                receipts={
                    **app.state.controls.state.receipts,
                    "crashed": {
                        "state": "pending",
                        "command": "/memory",
                        "digest": "unknown",
                    },
                }
            )
        app, api = control_runtime(tmp_path)
        async with app.router.lifespan_context(app):
            assert (await api.request("GET", "management"))["pending_operations"] == [
                {"id": "crashed", "command": "/memory"}
            ]
            assert (await command(api, "/memory", "note independent-input"))["ok"]
            assert (
                await command(api, "/management", "acknowledge crashed", confirmed=True)
            )["ok"]
            assert not (await api.request("GET", "management"))["pending_operations"]
            assert (await api.request("POST", "commands", request)) == results[0]

    asyncio.run(scenario())


def test_memory_approval_requires_the_reviewed_candidate_and_cannot_come_from_wechat(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path)
        async with app.router.lifespan_context(app):
            assert (await command(api, "/memory", "propose use short replies"))["ok"]
            original = (await api.request("GET", "management?section=memory"))["memory"]
            assert not (await command(api, "/memory", "approve"))["ok"]
            await command(api, "/memory", "reject")
            assert (await command(api, "/memory", "propose changed candidate"))["ok"]
            assert not (
                await command(
                    api,
                    "/memory",
                    "approve",
                    confirmed=True,
                    expected_proposal=original["proposal_digest"],
                )
            )["ok"]
            fresh = (await api.request("GET", "management?section=memory"))["memory"]
            assert not (
                await command(
                    api,
                    "/memory",
                    "approve",
                    source="wechat",
                    sender="owner",
                    account="bot-1",
                    confirmed=True,
                    expected_proposal=fresh["proposal_digest"],
                )
            )["ok"]
            assert (
                await command(
                    api,
                    "/memory",
                    "approve",
                    confirmed=True,
                    expected_proposal=fresh["proposal_digest"],
                )
            )["ok"]
            assert (
                "changed candidate"
                in (await api.request("GET", "management?section=memory"))["memory"][
                    "agent_md"
                ]
            )

    asyncio.run(scenario())


def test_management_is_authenticated_workspace_bound_and_same_origin(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(base_url=api.url, transport=api.transport) as browser,
        ):
            assert (await browser.get("/api/runtime/management")).status_code == 401
            response = await browser.post(
                "/api/login",
                json={"username": "cairn", "auth_passphrase": "private-test-token"},
            )
            assert response.status_code == 200
            payload = {
                "command": "/memory",
                "args": "note browser",
                "request_id": "browser",
            }
            assert (
                await browser.post("/api/runtime/commands", json=payload)
            ).status_code == 403
            assert (
                await browser.post(
                    "/api/runtime/commands",
                    json=payload,
                    headers={"Origin": "http://localhost:9999"},
                )
            ).status_code == 403
            assert (
                await browser.post(
                    "/api/runtime/commands", json=payload, headers={"Origin": api.url}
                )
            ).status_code == 200
            response = await browser.get(
                "/api/runtime/management",
                headers={"X-Cairn-Workspace": "file:///wrong"},
            )
            assert response.status_code == 409
            assert (
                "private-test-token"
                not in (
                    await browser.get("/api/runtime/management?section=memory")
                ).text
            )

    asyncio.run(scenario())


def test_wechat_identity_cannot_be_forged_or_redirected(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path)
        async with app.router.lifespan_context(app):
            for recipient, account in [
                ("stranger", "bot-1"),
                ("owner", "wrong-bot"),
                (None, "bot-1"),
            ]:
                with pytest.raises(httpx.HTTPStatusError):
                    await api.submit(
                        "hello",
                        "wechat",
                        mode="conversation",
                        delivery_recipient=recipient,
                        delivery_account=account,
                    )
            adapter = WechatRuntimeAdapter(api)
            assert "只有当前微信账号" in await adapter.reply(
                "/memory note bad", sender="stranger", account="bot-1"
            )
            assert not app.state.runtime.list_tasks()

    asyncio.run(scenario())


def test_wechat_login_and_runtime_final_reply_survive_client_detach(
    tmp_path: Path,
) -> None:
    async def scenario(fake: FakeILink) -> None:
        app, api = control_runtime(tmp_path)
        controls = app.state.controls
        fake.qr_statuses = [{"status": "need_verifycode"}, {"status": "confirmed"}]
        fake.confirm = {
            "bot_token": "private-bot-token",
            "ilink_bot_id": "bot-1",
            "baseurl": fake.url,
            "ilink_user_id": "owner",
        }
        controls.wechat.login_client = ILinkClient(
            base_url=fake.url, allow_insecure_local=True
        )
        controls.wechat.allow_insecure_local_http = True
        async with app.router.lifespan_context(app):
            cli = ServerCommands(api)
            assert (await cli.execute("/wx", "", None))["ok"]
            async with asyncio.timeout(5):
                while True:
                    state = (await api.request("GET", "management?section=wechat"))[
                        "wechat"
                    ]
                    if state["verification_required"]:
                        break
                    await asyncio.sleep(0.01)
            qr = state["qr_image"]
            assert qr.startswith("data:image/svg+xml;base64,")
            await cli.execute("/wx", "status", None)
            assert (await api.request("GET", "management?section=wechat"))["wechat"][
                "qr_image"
            ] == qr
            await cli.execute("/wx", "verify 123456", None)
            assert "123456" not in (tmp_path / "state" / "controls.json").read_text()
            async with asyncio.timeout(10):
                while not (await api.request("GET", "management?section=wechat"))[
                    "wechat"
                ]["running"]:
                    await asyncio.sleep(0.02)
            # No terminal input/polling task owns this receiver.
            del cli
            status = (await api.request("GET", "management?section=wechat"))["wechat"]
            assert (
                status["account_saved"]
                and status["running"]
                and status["qr_image"] is None
            )
            assert "private-bot-token" not in json.dumps(status)
            fake.updates.append({"ret": 0, "msgs": [_msg("你好", sender="owner")]})
            async with asyncio.timeout(15):
                while not any(
                    "离线共享对话已连接" in json.dumps(row, ensure_ascii=False)
                    for row in fake.sent
                ):
                    await asyncio.sleep(0.02)
            conversations = app.state.conversations.list_conversations()
            assert len(conversations) == 1
            runs = app.state.conversations.runs_for_conversation(
                conversations[0].conversation_id
            )
            assert len(runs) == 1 and runs[0].status.value == "completed"
            assert app.state.runtime.list_tasks() == []
            assert len(fake.sent) == 1
            assert "离线共享对话已连接" in json.dumps(fake.sent, ensure_ascii=False)
            binding = await api.get_wechat_conversation("bot-1", "owner")
            assert binding["conversation_id"] == conversations[0].conversation_id
            status_text = await WechatConversationAdapter(api).reply(
                "/status", sender="owner", account="bot-1", message_id="status-message"
            )
            assert "completed" in status_text
            intents = WechatState(tmp_path / "state" / "wechat").list_delivery_intents()
            assert len(intents) == 1
            assert intents[0]["admission_status"] == "accepted"
            assert intents[0]["delivery_status"] == "sent"
            assert intents[0]["conversation_id"] == conversations[0].conversation_id
            assert intents[0]["run_id"] == runs[0].run_id
            await command(api, "/wx", "stop")
            assert not (await api.request("GET", "management?section=wechat"))[
                "wechat"
            ]["running"]
        await controls.wechat.login_client.close()

    with FakeILink() as fake:
        asyncio.run(scenario(fake))


def test_schedule_commands_use_the_same_runtime_and_reminders_do_not_call_models(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path)
        deliveries: list[str] = []

        async def deliver(recipient: str, text: str, account: str) -> bool:
            assert (recipient, account) == ("owner", "bot-1")
            deliveries.append(text)
            return True

        async with app.router.lifespan_context(app):
            runner = app.state.controls.schedules.runner
            runner.deliver_for_account = deliver
            when = (current_time() + timedelta(minutes=2)).isoformat()
            assert (await command(api, "/schedule", f"remind once {when} drink water"))[
                "ok"
            ]
            assert (
                await command(api, "/schedule", f"add once {when} inspect project")
            )["ok"]
            jobs = (await api.request("GET", "management?section=schedule"))[
                "schedule"
            ]["jobs"]
            assert len(jobs) == 2 and {row["reminder"] for row in jobs} == {True, False}
            await runner.run_due(current_time() + timedelta(minutes=3))
            assert len(deliveries) == 2 and "drink water" in deliveries
            tasks = app.state.runtime.list_tasks()
            assert (
                len(tasks) == 1
                and tasks[0].source == "schedule"
                and tasks[0].status == "completed"
            )
            await runner.run_due(current_time() + timedelta(minutes=4))
            assert len(app.state.runtime.list_tasks()) == 1 and len(deliveries) == 2

    asyncio.run(scenario())


def test_slow_memory_management_does_not_block_status_or_chat(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import threading

    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        entered, release = threading.Event(), threading.Event()
        execute = app.state.controls.memory_controller.execute

        def slow(args: str) -> Any:
            entered.set()
            assert release.wait(5)
            return execute(args)

        monkeypatch.setattr(app.state.controls.memory_controller, "execute", slow)
        async with app.router.lifespan_context(app):
            operation = asyncio.create_task(command(api, "/memory", "note slowly"))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                await asyncio.wait_for(
                    api.request("GET", "management?section=wechat"), 0.5
                )
                task = await asyncio.wait_for(
                    api.submit("hello", "cli", mode="conversation"), 0.5
                )
                assert task["status"] == "queued"
            finally:
                release.set()
                await operation

    asyncio.run(scenario())


def test_agent_schedule_tool_and_cli_share_the_real_store(tmp_path: Path) -> None:
    from cairn.core.models import LLMResponse, Message, ToolCall

    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path)
        when = (current_time() + timedelta(days=1)).isoformat()

        class Model:
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                if messages[-1].role == "user":
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id=f"schedule-{len(messages)}",
                                name="schedule_create",
                                arguments={
                                    "prompt": "散步"
                                    if "散步" in (messages[-1].content or "")
                                    else "喝水",
                                    "kind": "once",
                                    "when": when,
                                    "reminder": True,
                                },
                            )
                        ]
                    )
                assert '"exit_code": 0' in (messages[-1].content or "")
                return LLMResponse(content="已创建提醒")

        app.state.execution.llm_factory = Model
        async with app.router.lifespan_context(app):
            task = await api.submit(
                "明天提醒我喝水",
                "wechat",
                mode="conversation",
                delivery_recipient="owner",
                delivery_account="bot-1",
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 10)
            task = await api.inspect(task["task_id"])
            assert task["status"] == "completed" and task["worktree"] is None
            jobs = (await api.request("GET", "management?section=schedule"))[
                "schedule"
            ]["jobs"]
            assert (
                len(jobs) == 1 and jobs[0]["prompt"] == "喝水" and jobs[0]["reminder"]
            )
            assert "喝水" in (await command(api, "/schedule", "list"))["message"]
            await api.message(
                task["task_id"],
                "明天再提醒我散步",
                source="wechat",
                request_id="second-reminder",
                expected_sequence=task["sequence"],
                delivery_recipient="owner",
                delivery_account="bot-1",
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 10)
            final = await api.inspect(task["task_id"])
            assert (
                final["status"] == "completed"
                and final["input_id"] == "second-reminder"
            )
            jobs = (await api.request("GET", "management?section=schedule"))[
                "schedule"
            ]["jobs"]
            assert {job["prompt"] for job in jobs} == {"喝水", "散步"}
            await api.message(
                task["task_id"],
                "明天再提醒我散步",
                source="wechat",
                request_id="second-reminder",
                expected_sequence=task["sequence"],
                delivery_recipient="owner",
                delivery_account="bot-1",
            )
            assert (
                len(
                    (await api.request("GET", "management?section=schedule"))[
                        "schedule"
                    ]["jobs"]
                )
                == 2
            )

    asyncio.run(scenario())


def test_model_selection_stays_inside_host_approved_group(tmp_path: Path) -> None:
    from cairn.core.context import ContextBuilder
    from cairn.core.model_manager import ModelConfig, ModelManager, ProviderConfig
    from cairn.core.provider_catalog import NamedProvider
    from cairn.runtime.model_executor import ModelExecutor
    from cairn.runtime.offline_model import OfflineDevelopmentModel
    from cairn.runtime.provider_runtime import ProviderRuntime

    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        built: list[str] = []
        executed: list[str] = []
        config = ProviderConfig(
            "https://example.invalid",
            "KEY",
            (
                ModelConfig("approved", ("first", "second")),
                ModelConfig("not-approved", ("other",)),
            ),
        )
        manager = ModelManager(config)

        def factory(ident: str) -> Any:
            built.append(ident)

            class Selected(OfflineDevelopmentModel):
                async def generate(self, messages: Any, tools: Any = None) -> Any:
                    executed.append(ident)
                    return await super().generate(messages, tools)

            return Selected(), ContextBuilder()

        llm, context = factory("first")
        app.state.execution.provider_runtime = ProviderRuntime(
            NamedProvider("test", config),
            manager,
            ModelExecutor(manager, factory),
            llm,
            context,
            factory,
        )
        async with app.router.lifespan_context(app):
            assert not (await command(api, "/model", "use other"))["ok"]
            assert (await command(api, "/model", "use second"))["ok"]
            task = await api.submit("hello", "cli", mode="conversation")
            await asyncio.wait_for(app.state.runtime.queue.join(), 10)
            assert (await api.inspect(task["task_id"]))["status"] == "completed"
            assert executed == ["second"]
            assert "other" not in built
            assert (
                manager.current_model_id() == "first"
            )  # No global in-flight binding changed.
            assert (await api.inspect(task["task_id"]))["model_id"] == "second"

    asyncio.run(scenario())


def test_disconnected_control_client_does_not_cancel_owned_write(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import threading

    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        entered, release = threading.Event(), threading.Event()
        execute = app.state.controls.memory_controller.execute

        def slow(args: str) -> Any:
            entered.set()
            assert release.wait(5)
            return execute(args)

        monkeypatch.setattr(app.state.controls.memory_controller, "execute", slow)
        request = {
            "command": "/memory",
            "args": "note survives-disconnect",
            "request_id": "detach",
        }
        async with app.router.lifespan_context(app):
            sending = asyncio.create_task(api.request("POST", "commands", request))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                sending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await sending
            finally:
                release.set()
            assert (await api.request("POST", "commands", request))["ok"]
            assert "survives-disconnect" in app.state.controls.memory.render_context()

    asyncio.run(scenario())


def test_task_board_commands_are_visible_in_web_without_starting_agents(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        async with app.router.lifespan_context(app):
            assert (await command(api, "/task", "add migration checklist"))["ok"]
            item = (await app.state.runtime.board.alist())[0]
            assert (await command(api, "/task", f"todo {item.task_id} verify CLI"))[
                "ok"
            ]
            snapshot = await api.request("GET", "../snapshot")
            assert snapshot["tasks"][0]["title"] == "migration checklist"
            assert not app.state.runtime.list_tasks()

    asyncio.run(scenario())


def test_memory_journaling_does_not_delay_wechat_final_reply(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import threading

    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path)
        release, entered = threading.Event(), threading.Event()
        delivered = asyncio.Event()
        record = app.state.controls.memory.record_task

        def slow(**kwargs: Any) -> None:
            entered.set()
            assert release.wait(5)
            record(**kwargs)

        async def deliver(recipient: str, text: str, account: str) -> bool:
            assert (recipient, account) == ("owner", "bot-1")
            delivered.set()
            return True

        monkeypatch.setattr(app.state.controls.memory, "record_task", slow)
        monkeypatch.setattr(app.state.controls.wechat, "deliver_for_account", deliver)
        async with app.router.lifespan_context(app):
            try:
                await api.submit(
                    "hello",
                    "wechat",
                    mode="conversation",
                    delivery_recipient="owner",
                    delivery_account="bot-1",
                )
                assert await asyncio.to_thread(entered.wait, 2)
                await asyncio.wait_for(delivered.wait(), 0.5)
            finally:
                release.set()

    asyncio.run(scenario())


def test_server_cleanup_finishes_after_repeated_cancellation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    async def scenario() -> None:
        app, _ = control_runtime(tmp_path)
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        entered, release = asyncio.Event(), asyncio.Event()
        close = app.state.controls.wechat.close

        async def slow_close() -> None:
            entered.set()
            await release.wait()
            await close()

        monkeypatch.setattr(app.state.controls.wechat, "close", slow_close)
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await entered.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 3)
        assert app.state.runtime.worker.done()
        assert app.state.controls.memory_worker.done()
        from cairn.infra.runtime_catalog import RuntimeCatalogue

        reopened = RuntimeCatalogue(
            tmp_path / "state",
            forbidden_roots=(tmp_path / "source", tmp_path / "worktrees"),
        )
        reopened.close()

    asyncio.run(scenario())


def test_server_restart_restores_one_receiver_and_saved_account(tmp_path: Path) -> None:
    async def scenario(fake: FakeILink) -> None:
        save_account(tmp_path, fake.url)
        for _ in range(2):
            app, api = control_runtime(tmp_path)
            app.state.controls.wechat.allow_insecure_local_http = True
            app.state.controls._save(wechat_enabled=True)
            prior = sum("getupdates" in row["path"] for row in fake.requests)
            async with app.router.lifespan_context(app):
                async with asyncio.timeout(5):
                    while (
                        sum("getupdates" in row["path"] for row in fake.requests)
                        <= prior
                    ):
                        await asyncio.sleep(0.01)
                status = (await api.request("GET", "management?section=wechat"))[
                    "wechat"
                ]
                assert (
                    status["running"] and status["enabled"] and status["account_saved"]
                )
            assert not app.state.controls.wechat.management_status()["running"]
        assert json.loads((tmp_path / "state" / "controls.json").read_text())[
            "wechat_enabled"
        ]

    with FakeILink() as fake:
        asyncio.run(scenario(fake))


def test_cli_approval_uses_the_displayed_candidate_and_checks_again_inside_write(
    tmp_path: Path, monkeypatch: Any
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        async with app.router.lifespan_context(app):
            controls = app.state.controls
            await command(api, "/memory", "propose reviewed rule")
            cli = ServerCommands(api)
            response = await cli.execute("/memory", "pending", None)
            assert cli.proposal == response["proposal_digest"]
            execute = controls.memory_controller.execute

            def concurrent_change(args: str, **kwargs: Any) -> Any:
                controls.memory.reject_agent_update()
                controls.memory.propose_agent_update(
                    "unreviewed replacement", "other client"
                )
                return execute(args, **kwargs)

            monkeypatch.setattr(
                controls.memory_controller, "execute", concurrent_change
            )
            result = await cli.execute("/memory", "approve", None)
            assert not result["ok"]
            assert "unreviewed replacement" not in controls.memory.store.read_agent_md()
            assert (
                controls.memory.pending_proposal()["text"] == "unreviewed replacement"
            )

    asyncio.run(scenario())


def test_wechat_delivery_failure_is_recorded_without_reexecution(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path)
        async with app.router.lifespan_context(app):
            controls = app.state.controls

            async def fail_delivery(*_args: Any, **_kwargs: Any) -> bool:
                return False

            controls.wechat.deliver_for_account = fail_delivery
            task = await api.submit(
                "hello",
                "wechat",
                mode="conversation",
                delivery_recipient="owner",
                delivery_account="bot-1",
            )
            await asyncio.wait_for(app.state.runtime.queue.join(), 10)
            for _ in range(100):
                if not controls.delivery_tasks:
                    break
                await asyncio.sleep(0.01)
            record = controls.state.deliveries.get(task["task_id"] + ":1")
            assert record == "unconfirmed"
            assert len(app.state.runtime.list_tasks()) == 1

    asyncio.run(scenario())
