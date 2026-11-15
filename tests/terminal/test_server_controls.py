"""Public clients exercise the same Server-owned controls, without paid APIs."""

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

from cairn.integrations.wechat import session as wechat_session
from cairn.integrations.wechat.ilink import ILinkClient
from cairn.integrations.wechat.runtime import (
    WechatConversationAdapter,
    WechatRuntimeAdapter,
)
from cairn.integrations.wechat.state import WechatAccount, WechatState
from cairn.interfaces.cli.input import CliInput
from cairn.interfaces.cli.runtime_chat import main as shared_main
from cairn.interfaces.cli.server_commands import ServerCommands
from cairn.scheduling.runner import current_time
from cairn.shared.delivery import ProactiveSendOutcome, ProactiveSendResult
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


def test_daily_reminder_records_ilink_rejection_and_uses_new_inbound_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wechat_session, "_REMINDER_RETRY_DELAYS", (0, 0))

    class ReminderILink(FakeILink):
        malformed = False

        def handle(
            self, method: str, path: str, headers: Any, body: bytes
        ) -> dict[str, Any]:
            result = super().handle(method, path, headers, body)
            if self.malformed and path == "/ilink/bot/sendmessage":
                return {"ret": "private-upstream-data"}
            return result

    async def scenario(fake: ReminderILink) -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path, fake.url)
        controls = app.state.controls
        controls.wechat.allow_insecure_local_http = True
        runner = controls.schedules.runner
        state = controls.wechat.state

        async def run_next() -> tuple[str, str | None]:
            (job,) = runner.store.load_jobs()
            when = job.next_run_at
            assert when is not None
            runner.clock = lambda: when
            await runner.run_reminders(when)
            snapshot = (await api.request("GET", "management?section=schedule"))[
                "schedule"
            ]
            run = snapshot["runs"][0]
            assert run["run_key"] == f"{job.job_id}:{when.isoformat()}"
            (persisted,) = snapshot["jobs"]
            assert persisted["next_run_at"] == (when + timedelta(days=1)).isoformat()
            assert persisted["last_status"] == run["status"]
            assert persisted["last_error"] == run["error"]
            return str(run["status"]), run["error"]

        async with app.router.lifespan_context(app):
            assert (await command(api, "/schedule", "remind daily 08:00 gym"))["ok"]
            assert await run_next() == (
                "delivery_failed",
                "delivery_unavailable: no_context_token",
            )
            assert fake.sent == []

            state.record_received(
                "old", sender="owner", context_token="private-old-context", text="hi"
            )
            fake.send_ret = -2
            assert await run_next() == (
                "delivery_failed",
                "delivery_rejected: ilink_ret=-2",
            )
            assert len(fake.sent) == 1
            (job,) = runner.store.load_jobs()
            history = await command(api, "/schedule", f"history {job.job_id}")
            assert "ilink_ret=-2" in history["message"]
            assert "private-old-context" not in history["message"]

            state.record_received(
                "new", sender="owner", context_token="private-new-context", text="hi"
            )
            state.mark("old", "completed")  # Old work settles after the new inbound.
            fake.malformed = True
            assert await run_next() == ("delivery_unknown", "delivery_unconfirmed")
            assert len(fake.sent) == 2
            assert fake.sent[-1]["context_token"] == "private-new-context"

        app, api = control_runtime(tmp_path)
        controls = app.state.controls
        controls.wechat.allow_insecure_local_http = True
        runner = controls.schedules.runner
        fake.malformed = False
        fake.send_ret = 0
        async with app.router.lifespan_context(app):
            unknown = runner.store.load_runs()[0]
            assert unknown["status"] == "delivery_unknown"
            assert unknown["delivered"] is False
            (job,) = runner.store.load_jobs()
            assert job.next_run_at is not None
            await runner.run_reminders(job.next_run_at - timedelta(seconds=1))
            assert len(fake.sent) == 2
            assert await run_next() == ("completed", None)
            assert len(fake.sent) == 3
            assert fake.sent[-1]["context_token"] == "private-new-context"

    fake = ReminderILink()
    with fake:
        asyncio.run(scenario(fake))


@pytest.mark.parametrize(
    ("replies", "change", "expected_status", "expected_contexts"),
    [
        (["connect", "connect", "connect"], None, "delivery_failed", ["old"] * 3),
        (["connect", "connect", 0], None, "completed", ["old"] * 3),
        (["connect", "read_timeout"], None, "delivery_unknown", ["old"] * 2),
        ([-2], None, "delivery_failed", ["old"]),
        ([-2, 0], "context", "completed", ["old", "new"]),
        ([7], "context", "delivery_failed", ["old"]),
        (["connect"], "pause", "delivery_failed", ["old"]),
        (["connect"], "account", "delivery_failed", ["old"]),
    ],
)
def test_reminder_retry_uses_only_safe_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: list[int | str],
    change: str | None,
    expected_status: str,
    expected_contexts: list[str],
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path)
        controls = app.state.controls
        runner = controls.schedules.runner
        state = controls.wechat.state
        state.record_received("old", sender="owner", context_token="old", text="hi")
        contexts: list[str] = []
        monkeypatch.setattr(wechat_session, "_REMINDER_RETRY_DELAYS", (0, 0))

        async def send(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path == "/ilink/bot/sendmessage"
            reply = replies[len(contexts)]
            msg = json.loads(await request.aread())["msg"]
            assert msg["to_user_id"] == "owner"
            contexts.append(msg["context_token"])
            if len(contexts) == 1:
                if change == "context":
                    state.record_received(
                        "new", sender="owner", context_token="new", text="hi again"
                    )
                elif change == "pause":
                    (job,) = runner.store.load_jobs()
                    assert (await command(api, "/schedule", f"pause {job.job_id}"))[
                        "ok"
                    ]
                elif change == "account":
                    state.save_account(
                        WechatAccount(
                            "private-new-token",
                            "other-bot",
                            "https://example.invalid",
                            "owner",
                        )
                    )
            if reply == "connect":
                raise httpx2.ConnectError("private transport detail", request=request)
            if reply == "read_timeout":
                raise httpx2.ReadTimeout("private transport detail", request=request)
            return httpx2.Response(200, json={"ret": reply})

        def client(**kwargs: Any) -> ILinkClient:
            return ILinkClient(**kwargs, transport=httpx2.MockTransport(send))

        monkeypatch.setattr(wechat_session, "ILinkClient", client)
        async with app.router.lifespan_context(app):
            assert (await command(api, "/schedule", "remind daily 08:00 gym"))["ok"]
            (job,) = runner.store.load_jobs()
            when = job.next_run_at
            assert when is not None
            await runner.run_reminders(when)
            # The retry loop must not make another occurrence or replay this one.
            await runner.run_reminders(when)
            latest, started = runner.store.load_runs()
            assert latest["run_key"] == started["run_key"]
            assert latest["status"] == expected_status
            assert latest["delivery_attempts"] == len(expected_contexts)
            assert "private transport detail" not in str(latest["error"])
            assert contexts == expected_contexts
            if change != "account":
                history = await command(api, "/schedule", f"history {job.job_id}")
                assert f"attempts={len(expected_contexts)}" in history["message"]

    asyncio.run(scenario())


def test_shutdown_during_reminder_backoff_does_not_replay_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path)
        controls = app.state.controls
        controls.wechat.state.record_received(
            "message", sender="owner", context_token="context", text="hi"
        )
        runner = controls.schedules.runner
        sent = asyncio.Event()
        attempts = 0

        async def send(request: httpx2.Request) -> httpx2.Response:
            nonlocal attempts
            attempts += 1
            sent.set()
            raise httpx2.ConnectTimeout("fixture timeout", request=request)

        def client(**kwargs: Any) -> ILinkClient:
            return ILinkClient(**kwargs, transport=httpx2.MockTransport(send))

        monkeypatch.setattr(wechat_session, "ILinkClient", client)
        monkeypatch.setattr(wechat_session, "_REMINDER_RETRY_DELAYS", (60, 60))
        async with app.router.lifespan_context(app):
            assert (await command(api, "/schedule", "remind daily 08:00 gym"))["ok"]
            (job,) = runner.store.load_jobs()
            due = job.next_run_at
            assert due is not None
            task = asyncio.create_task(runner.run_reminders(due))
            try:
                await asyncio.wait_for(sent.wait(), timeout=2)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        app, _ = control_runtime(tmp_path)
        runner = app.state.controls.schedules.runner
        async with app.router.lifespan_context(app):
            await runner.run_due(due)
            assert attempts == 1
            finished, started = runner.store.load_runs()
            assert finished["run_key"] == started["run_key"]
            assert finished["status"] == "cancelled"

    asyncio.run(scenario())


def test_schedule_commands_use_the_same_runtime_and_reminders_do_not_call_models(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, api = control_runtime(tmp_path)
        save_account(tmp_path)
        deliveries: list[str] = []

        async def deliver(
            recipient: str, text: str, account: str
        ) -> ProactiveSendResult:
            assert (recipient, account) == ("owner", "bot-1")
            deliveries.append(text)
            return ProactiveSendResult(ProactiveSendOutcome.SENT)

        async with app.router.lifespan_context(app):
            runner = app.state.controls.schedules.runner

            async def deliver_reminder(
                recipient: str, text: str, account: str, _can_retry: Callable[[], bool]
            ) -> ProactiveSendResult:
                return await deliver(recipient, text, account)

            runner.deliver_for_account = deliver
            runner.deliver_reminder = deliver_reminder
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
    from cairn.agent.models.messages import LLMResponse, Message, ToolCall

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
    from cairn.agent.context.window import ContextBuilder
    from cairn.agent.models.catalog import NamedProvider
    from cairn.agent.models.executor import ModelExecutor
    from cairn.agent.models.offline import OfflineDevelopmentModel
    from cairn.agent.models.runtime import ProviderRuntime
    from cairn.agent.models.selection import ModelConfig, ModelManager, ProviderConfig

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
        assert app.state.controls.memory_journal.worker.done()
        from cairn.development.catalogue import RuntimeCatalogue

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


def test_wechat_task_commands_use_receipts_and_revalidate_authorization(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, _api = control_runtime(tmp_path)
        save_account(tmp_path)
        async with app.router.lifespan_context(app):
            controls = app.state.controls
            controls.state = controls.state.model_copy(
                update={"trusted_senders": ("u2",)}
            )
            task = app.state.runtime.submit(
                "projection test",
                "wechat",
                request_id="wx-task-submit",
                mode="development",
                delivery_recipient="u2",
                delivery_account="bot-1",
            )
            calls: list[dict[str, Any]] = []

            async def fake(request: dict[str, Any]) -> dict[str, Any]:
                calls.append(dict(request))
                current = app.state.runtime.inspect(request["task_id"])
                return {"ok": True, "task": current.model_dump(mode="json")}

            controls._wechat_task_command = fake
            request = {
                "command": "/reply",
                "args": "one input",
                "task_id": task.task_id,
                "request_id": "wx-command-1",
                "source": "wechat",
                "sender": "u2",
                "account": "bot-1",
            }
            first = await controls.command(request)
            second = await controls.command(dict(request))
            assert first == second
            assert len(calls) == 1

            with pytest.raises(ValueError, match="different input"):
                await controls.command({**request, "args": "different input"})

            # A revoked sender cannot use the historical receipt to read the
            # result.
            controls.state = controls.state.model_copy(update={"trusted_senders": ()})
            revoked = await controls.command(
                {
                    **request,
                    "request_id": "wx-command-revoked",
                    "args": "one input",
                }
            )
            assert revoked["ok"] is False

            # Concurrent identical requests share one operation.
            controls.state = controls.state.model_copy(
                update={"trusted_senders": ("u2",)}
            )
            concurrent = {
                **request,
                "request_id": "wx-command-concurrent",
                "args": "same input",
            }
            results = await asyncio.gather(
                controls.command(dict(concurrent)),
                controls.command(dict(concurrent)),
            )
            assert results[0] == results[1]
            assert len(calls) == 2

    asyncio.run(scenario())
