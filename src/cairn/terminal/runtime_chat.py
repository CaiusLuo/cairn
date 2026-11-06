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

from cairn.core.events import Event, EventType
from cairn.infra.runtime_client import RuntimeClient
from cairn.runtime.lifecycle import await_owned, settle_owned
from cairn.terminal.frame import TranscriptView, frame_live_region
from cairn.terminal.input import CliInput
from cairn.terminal.output import (
    StreamingDisplay,
    print_assistant_response,
    print_banner,
    print_user_message,
)
from cairn.terminal.server_commands import ServerCommands
from cairn.terminal.submission import PendingLine


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
    remote = RemoteConversation(api)
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

    def show(task: dict[str, Any]) -> None:
        print(f"{task['task_id']} · {task['status']}")

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
            remote.task = await api.inspect(remote.task["task_id"])
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
                    await api.control(remote.task["task_id"], "cancel")
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
                            show(await api.inspect(remote.task["task_id"]))
                        continue
                    if line.strip() == "/cancel":
                        stopped.set()
                    else:
                        pending.append(line)
                        print("Queued for the next shared turn.")
            task = waiting.result()
            show(task)
            display.close_incomplete(silent=True)
            if task.get("result"):
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
            show(remote.task)
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
                    remote = RemoteConversation(api)
                    print("Next input starts a new shared conversation.")
                    continue
                if command == "/develop":
                    if not argument.strip():
                        print(
                            "Use /develop GOAL to start an explicit development task."
                        )
                        continue
                    remote = RemoteConversation(api, mode="development")
                    show(await remote.send(argument))
                    if not await follow():
                        return
                    continue
                if command == "/use":
                    history = await remote.select(argument)
                    assert remote.task is not None
                    show(remote.task)
                    for message in history:
                        if message["role"] == "assistant" and message.get("content"):
                            print_assistant_response(message["content"])
                    continue
                if command in {"/status", "/history", "/continue", "/cancel"}:
                    if remote.task is None:
                        print("Select a task with /use ID first.")
                        continue
                    ident = remote.task["task_id"]
                    if command == "/history":
                        for item in await api.request("GET", f"tasks/{ident}/history"):
                            if item.get("content") and item["role"] in {
                                "user",
                                "assistant",
                            }:
                                print(f"{item['role']}: {item['content']}")
                        continue
                    remote.task = (
                        await api.inspect(ident)
                        if command == "/status"
                        else await api.control(ident, command[1:])
                    )
                    show(remote.task)
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
                print(
                    f"Shared Runtime request failed: {type(error).__name__}. Keep the task ID and inspect it before retrying."
                )
    finally:
        await settle_owned(wechat_watcher)
        if frame:
            closing = asyncio.create_task(frame.aclose())
            late = await await_owned(closing)
            closing.result()
            if late:
                raise late
