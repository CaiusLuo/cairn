"""Interactive terminal over Runtime facts; never creates an Agent or writer Store."""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from typing import Any, get_args
from uuid import uuid4

import httpx

from cairn.agent.turn.events import Event, EventType
from cairn.interfaces.cli.frame import TranscriptView, frame_live_region
from cairn.interfaces.cli.input import CliInput
from cairn.interfaces.cli.output import (
    StreamingDisplay,
    print_assistant_response,
    print_banner,
    print_user_message,
)
from cairn.interfaces.cli.server_commands import ServerCommands
from cairn.interfaces.cli.submission import PendingLine
from cairn.server.client import RuntimeClient
from cairn.shared.lifecycle import await_owned, settle_owned


class RemoteChat:
    """Conversation/Run client for ordinary chat; no task_id identity."""

    def __init__(self, api: RuntimeClient) -> None:
        self.api = api
        self.conversation: dict[str, Any] | None = None
        self.task: dict[str, Any] | None = None
        self.pending: tuple[str | None, str, str, int] | None = None
        self.conversation_request_id: str | None = None

    def _set_run(self, run: dict[str, Any]) -> dict[str, Any]:
        self.task = {
            **run,
            "task_id": run["run_id"],
            "display_id": run["conversation_id"],
            "sequence": run.get("cursor", 0),
        }
        return self.task

    async def select(self, ident: str) -> list[dict[str, Any]]:
        if ident.startswith("conv-"):
            detail = await self.api.get_conversation(ident)
            self.conversation = detail["conversation"]
            runs = detail.get("runs", [])
            if runs:
                self._set_run(dict(runs[-1], cursor=0))
            return await self.api.conversation_history(ident)
        if ident.startswith("run-"):
            run = await self.api.get_run(ident)
            self.conversation = (
                await self.api.get_conversation(run["conversation_id"])
            )["conversation"]
            self.task = self._set_run({**run, "cursor": 0})
            return await self.api.conversation_history(run["conversation_id"])
        raise ValueError("Use a Conversation or Run ID")

    async def send(
        self, prompt: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        if self.pending is not None:
            if self.pending[:2] != (
                self.conversation["conversation_id"] if self.conversation else None,
                prompt,
            ):
                raise ValueError(
                    "Retry the unacknowledged input before sending another message"
                )
            request_id = self.pending[2]
        else:
            request_id = request_id or "cli:" + uuid4().hex
            self.conversation_request_id = (
                self.conversation_request_id or "cli-conv:" + uuid4().hex
            )
            self.pending = (
                self.conversation["conversation_id"] if self.conversation else None,
                prompt,
                request_id,
                0,
            )
        try:
            if self.conversation is None:
                created = await self.api.create_conversation(
                    prompt,
                    source="cli",
                    request_id=self.conversation_request_id,
                )
                self.conversation = created
            run = await self.api.send_conversation(
                self.conversation["conversation_id"],
                prompt,
                source="cli",
                request_id=request_id,
            )
        except httpx.HTTPStatusError:
            self.pending = None
            raise
        self.pending = None
        self.conversation_request_id = None
        return self._set_run(run)

    async def wait(
        self, observe: Callable[[Event], object] | None = None
    ) -> dict[str, Any]:
        assert self.task is not None
        run_id = self.task["run_id"]
        cursor = int(self.task.get("cursor", 0))
        while True:
            page = await self.api.run_events(run_id, after=cursor, wait=True)
            for item in page["events"]:
                if observe is not None and item["kind"] in get_args(EventType):
                    observe(Event(type=item["kind"], data=item["data"]))
            cursor = int(page["cursor"])
            run = await self.api.get_run(run_id)
            self.task = self._set_run({**run, "cursor": cursor})
            final_cursor = int(run.get("cursor", cursor))
            if run["status"] not in {"queued", "running"} and cursor >= final_cursor:
                return self.task

    async def refresh(self) -> dict[str, Any]:
        assert self.task is not None
        run = await self.api.get_run(self.task["run_id"])
        self.task = self._set_run({**run, "cursor": self.task.get("cursor", 0)})
        return self.task

    async def history(self) -> list[dict[str, Any]]:
        if self.conversation is None:
            return []
        return await self.api.conversation_history(self.conversation["conversation_id"])

    async def control(self, action: str) -> dict[str, Any]:
        if action != "cancel":
            raise ValueError("Conversation runs only support cancel")
        assert self.task is not None
        run = await self.api.cancel_run(self.task["run_id"])
        return self._set_run({**run, "cursor": self.task.get("cursor", 0)})


class RemoteDevelopment:
    """CLI cursor for one explicit Development request.

    The pending tuple is deliberately kept until a successful response. A
    retry of the same prompt/conversation reuses the original request_id, so a
    lost response cannot silently create a second Task.
    """

    def __init__(self, api: RuntimeClient) -> None:
        self.api = api
        self.pending: tuple[str, str | None, str] | None = None
        self.task: dict[str, Any] | None = None

    async def start(
        self,
        prompt: str,
        *,
        conversation_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if self.pending is not None:
            pending_prompt, pending_conversation, pending_request = self.pending
            if pending_prompt != prompt or pending_conversation != conversation_id:
                raise ValueError(
                    "Retry the unacknowledged development request before starting another"
                )
            request_id = pending_request
        else:
            request_id = request_id or "cli-develop:" + uuid4().hex
            self.pending = (prompt, conversation_id, request_id)
        try:
            created = await self.api.create_development(
                prompt,
                "cli",
                request_id,
                conversation_id=conversation_id,
            )
            task = created.get("task")
            if not isinstance(task, dict) or "task_id" not in task:
                task = await self.api.inspect(created["task_id"])
        except httpx.HTTPStatusError as error:
            if 400 <= error.response.status_code < 500:
                self.pending = None
            raise
        self.task = task
        self.pending = None
        return task


class RemoteConversation:
    """A client cursor, not a second execution lifecycle."""

    def __init__(self, api: RuntimeClient, *, mode: str = "conversation") -> None:
        self.mode = mode
        self.api = api
        self.task: dict[str, Any] | None = None
        self.pending: tuple[str | None, str, str, int] | None = None

    async def select(self, task_id: str) -> list[dict[str, Any]]:
        self.task = await self.api.inspect(task_id)
        self.pending = None
        return list(await self.api.request("GET", f"tasks/{task_id}/history"))

    async def refresh(self) -> dict[str, Any]:
        assert self.task is not None
        self.task = await self.api.inspect(self.task["task_id"])
        return self.task

    async def history(self) -> list[dict[str, Any]]:
        assert self.task is not None
        return list(
            await self.api.request("GET", f"tasks/{self.task['task_id']}/history")
        )

    async def control(self, action: str) -> dict[str, Any]:
        assert self.task is not None
        self.task = await self.api.control(self.task["task_id"], action)
        return self.task

    async def send(
        self, prompt: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        ident = self.task["task_id"] if self.task else None
        sequence = self.task["sequence"] if self.task else 0
        if self.pending is not None:
            if self.pending[:2] != (ident, prompt):
                raise ValueError(
                    "Retry the unacknowledged input before sending another message"
                )
            _, _, request_id, sequence = self.pending
        else:
            request_id = request_id or "cli:" + uuid4().hex
            self.pending = (ident, prompt, request_id, sequence)
        try:
            if self.task is None:
                self.task = await self.api.submit(
                    prompt, "cli", request_id, mode=self.mode
                )
            else:
                self.task = await self.api.message(
                    self.task["task_id"],
                    prompt,
                    source="cli",
                    request_id=request_id,
                    expected_sequence=sequence,
                )
        except httpx.HTTPStatusError as error:
            if 400 <= error.response.status_code < 500:
                self.pending = None  # A deterministic refusal did not accept input.
                detail = None
                with suppress(Exception):
                    detail = error.response.json().get("error")
                if detail in {
                    "task_busy",
                    "task_recovery_required",
                    "task_not_continuable",
                }:
                    raise ValueError(
                        {
                            "task_busy": "Task is still executing; observe this turn before appending",
                            "task_recovery_required": "Task requires host recovery review before another input",
                            "task_not_continuable": "Task cannot accept another input; start a new Task",
                        }[detail]
                    ) from error
            raise
        self.pending = None
        return self.task

    async def wait(
        self, observe: Callable[[Event], object] | None = None
    ) -> dict[str, Any]:
        assert self.task is not None
        ident = self.task["task_id"]
        cursor = self.task["sequence"]
        while True:
            page = await self.api.request(
                "GET", f"tasks/{ident}/events?after={cursor}&wait=1"
            )
            for item in page["events"]:
                if observe is not None and item["kind"] in get_args(EventType):
                    observe(Event(type=item["kind"], data=item["data"]))
            cursor = page["cursor"]
            self.task = await self.api.inspect(ident)
            # Drain every durable page before returning the terminal state.
            if (
                self.task["status"] not in {"queued", "running"}
                and cursor == self.task["sequence"]
            ):
                return self.task


async def main(
    api: RuntimeClient,
    *,
    task_id: str | None = None,
    cli_input: CliInput | None = None,
    frame: TranscriptView | None = None,
) -> None:
    remote: RemoteChat | RemoteConversation = (
        RemoteChat(api)
        if task_id is None or task_id.startswith(("conv-", "run-"))
        else RemoteConversation(api)
    )
    development = RemoteDevelopment(api)
    try:
        catalog = await api.request("GET", "commands")
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise ValueError(
                "Server 仍在运行旧版本, 请先重启 cairn 再连接 cairn-cli。"
            ) from None
        raise
    reader = cli_input or CliInput()
    if (
        frame is None
        and cli_input is None
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    ):
        frame = TranscriptView()
    if frame is not None:
        frame.steering_enabled = False
    display = StreamingDisplay(
        live_factory=frame_live_region(frame) if frame else None,
        on_status=frame.observe_status if frame else None,
    )
    pending: deque[str] = deque()
    controls = ServerCommands(api)
    if frame is not None:
        frame.set_command_catalog(catalog)
    control_names = {row["name"] for row in catalog} - {
        "/new",
        "/develop",
        "/use",
        "/status",
        "/history",
        "/continue",
        "/cancel",
        "/exit",
    }
    control_names |= {"/trace", "/management"}
    wechat_watcher = asyncio.create_task(controls.watch_wechat())
    print_banner()
    print("Shared Runtime · 已连接共享 Server · /help 查看命令 · /exit 退出客户端")

    async def read() -> str:
        value = await frame.read() if frame else await reader.read()
        return value.text if isinstance(value, PendingLine) else value

    def show(task: dict[str, Any], *, explicit: bool = False) -> None:
        status = str(task.get("status", "unknown"))
        if explicit:
            print(f"{task.get('display_id', task['task_id'])} · {status}")
        elif status not in {"queued", "running", "completed"}:
            detail = task.get("error") or task.get("diagnostic") or ""
            print(f"{status}: {detail}" if detail else status)

    async def manage(line: str, *, following: bool = False) -> bool:
        command, _, argument = line.strip().partition(" ")
        if command not in control_names:
            return False
        if following and command == "/task" and argument.split()[:1] == ["use"]:
            print("当前正在跟随执行, 请先退出客户端, 再用 --task-id 连接另一个任务。")
            return True
        result = await controls.execute(
            command, argument, remote.task["task_id"] if remote.task else None
        )
        if (
            command == "/task"
            and result.get("task_id")
            and argument.split()[:1] == ["use"]
        ):
            await remote.select(result["task_id"])
        elif remote.task:
            remote.task = await remote.refresh()
        return True

    async def follow() -> bool:
        display.reset()
        stopped = asyncio.Event()
        if frame:
            frame.set_running(True)
            frame.on_interrupt = stopped.set
        waiting = asyncio.create_task(remote.wait(display.observe))
        reading: asyncio.Task[str] | None = None
        cancelling = asyncio.create_task(stopped.wait())
        try:
            while not waiting.done():
                if frame is not None and reading is None:
                    reading = asyncio.create_task(read())
                watched = [waiting, cancelling]
                if reading is not None:
                    watched.append(reading)
                done, _ = await asyncio.wait(
                    watched, return_when=asyncio.FIRST_COMPLETED
                )
                if cancelling in done:
                    assert remote.task is not None
                    await remote.control("cancel")
                    stopped.clear()
                    cancelling = asyncio.create_task(stopped.wait())
                if reading is not None and reading in done:
                    try:
                        line = reading.result()
                    except EOFError:
                        return False  # Detach; the service owns execution.
                    reading = None
                    if line.strip() in {"/exit", "/quit"}:
                        return False
                    if await manage(line, following=True):
                        continue
                    if line.strip() == "/status":
                        if remote.task:
                            show(await remote.refresh(), explicit=True)
                        continue
                    if line.strip() == "/cancel":
                        stopped.set()
                    else:
                        pending.append(line)
                        print("Queued for the next shared turn.")
            task = waiting.result()
            show(task)
            display.close_incomplete(silent=True)
            if task.get("result") and not display.final_text_visible:
                print_assistant_response(task["result"])
            if task.get("diagnostic"):
                print(task["diagnostic"])
            return True
        finally:
            # A submitted line racing completion must never disappear.
            if reading is not None and reading.done() and not reading.cancelled():
                with suppress(EOFError):
                    pending.append(reading.result())
            await settle_owned(
                *(t for t in (waiting, reading, cancelling) if t is not None)
            )
            if frame:
                frame.set_running(False)
                frame.on_interrupt = None
                pending.extend(message.text for message in frame.steering.drain())
            display.close_incomplete(silent=True)

    try:
        if task_id is not None:
            for message in await remote.select(task_id):
                if message["role"] == "assistant" and message.get("content"):
                    print_assistant_response(message["content"])
            assert remote.task is not None
            show(remote.task, explicit=True)
            if remote.task["status"] in {"queued", "running"} and not await follow():
                return
        while True:
            try:
                line = pending.popleft() if pending else await read()
            except KeyboardInterrupt:
                continue
            except EOFError:
                return
            if not line.strip():
                continue
            command, _, argument = line.strip().partition(" ")
            try:
                if command in {"/exit", "/quit"}:
                    return
                if command == "/new":
                    remote = RemoteChat(api)
                    print("Next input starts a new shared conversation.")
                    continue
                if command == "/develop":
                    if not argument.strip():
                        print(
                            "Use /develop GOAL to start an explicit development task."
                        )
                        continue
                    conversation_id = (
                        remote.conversation["conversation_id"]
                        if isinstance(remote, RemoteChat) and remote.conversation
                        else None
                    )
                    try:
                        task = await development.start(
                            argument, conversation_id=conversation_id
                        )
                    except ValueError as error:
                        print(str(error))
                        continue
                    except httpx.HTTPError as error:
                        if development.pending is not None:
                            print(
                                "Development request unconfirmed; retry the same goal with "
                                f"/develop {argument} ({type(error).__name__})."
                            )
                        else:
                            print(
                                f"Development request rejected: {type(error).__name__}."
                            )
                        continue
                    remote = RemoteConversation(api, mode="development")
                    remote.task = task
                    show(remote.task, explicit=True)
                    if not await follow():
                        return
                    continue
                if command == "/use":
                    history = await remote.select(argument)
                    assert remote.task is not None
                    show(remote.task, explicit=True)
                    for message in history:
                        if message["role"] == "assistant" and message.get("content"):
                            print_assistant_response(message["content"])
                    continue
                if command in {"/status", "/history", "/continue", "/cancel"}:
                    if remote.task is None:
                        print("Select a task with /use ID first.")
                        continue
                    if command == "/history":
                        for item in await remote.history():
                            if item.get("content") and item["role"] in {
                                "user",
                                "assistant",
                            }:
                                print(f"{item['role']}: {item['content']}")
                        continue
                    remote.task = (
                        await remote.refresh()
                        if command == "/status"
                        else await remote.control(command[1:])
                    )
                    show(remote.task, explicit=command == "/status")
                    if (
                        remote.task["status"] in {"queued", "running"}
                        and not await follow()
                    ):
                        return
                    continue
                if await manage(line):
                    continue
                if command.startswith("/"):
                    print("未知命令, 使用 /help 查看当前 Server 支持的操作。")
                    continue
                print_user_message(line)
                show(await remote.send(line))
                if not await follow():
                    return
            except (ValueError, RuntimeError, httpx.HTTPError) as error:
                detail = (
                    str(error)
                    if isinstance(error, (ValueError, RuntimeError))
                    else type(error).__name__
                )
                print(
                    f"Shared Runtime request failed: {detail}. Keep the task ID and inspect it before retrying."
                )
    finally:
        await settle_owned(wechat_watcher)
        if frame:
            closing = asyncio.create_task(frame.aclose())
            late = await await_owned(closing)
            closing.result()
            if late:
                raise late
