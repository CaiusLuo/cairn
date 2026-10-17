"""Experimental, single-client JSONL adapter. Runtime remains the turn owner."""

import asyncio
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from typing import Any, BinaryIO, TextIO
from uuid import uuid4

from dotenv import dotenv_values

from cairn.core.events import Event, SessionEvent
from cairn.core.models import ToolCall
from cairn.core.permissions import (
    PermissionChoice,
    PermissionRequest,
    PermissionResult,
    SessionPermissionHandler,
)
from cairn.core.provider_catalog import NamedProvider, ProviderCatalog
from cairn.infra.config import (
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.lifecycle import SettleOutcome, await_owned, settle_owned
from cairn.runtime.loop import run_turn
from cairn.runtime.provider_runtime import (
    ProviderRuntime,
    ProviderSession,
    build_provider_runtime,
)
from cairn.runtime.session import Session
from cairn.terminal.cli import DEFAULT_CLI_RUN_BUDGET

MAX_LINE = 65536
APPROVAL_SECONDS = 120.0


def decode_request(line: bytes) -> dict[str, Any]:
    if not line.endswith(b"\n"):
        raise ValueError("incomplete line")
    value = json.loads(line)
    if (
        not isinstance(value, dict)
        or type(value.get("v")) is not int
        or value["v"] != 1
    ):
        raise ValueError("protocol version")
    if type(value.get("id")) is not int or not 0 < value["id"] <= 2**53 - 1:
        raise ValueError("request ID")
    fields: dict[str, dict[str, Any]] = {
        "init": {},
        "shutdown": {},
        "prompt": {"text": str},
        "model": {"name": str},
        "provider": {"name": str},
        "cancel": {"request_id": int, "turn_id": (str, type(None))},
        "approval": {
            "approval_id": str,
            "request_id": int,
            "turn_id": (str, type(None)),
            "choice": str,
        },
    }
    op = value.get("op")
    if not isinstance(op, str) or op not in fields:
        raise ValueError("operation")
    spec = fields[op]
    if set(value) != {"v", "id", "op", *spec}:
        raise ValueError("fields")
    for key, expected in spec.items():
        if not isinstance(value[key], expected) or isinstance(value[key], bool):
            raise ValueError("field type")
    if (
        op in {"prompt", "provider", "model"}
        and not value["text" if op == "prompt" else "name"].strip()
    ):
        raise ValueError("empty input")
    if op == "approval" and value["choice"] not in {c.value for c in PermissionChoice}:
        raise ValueError("choice")
    return value


class Bridge:
    def __init__(self, source: BinaryIO, output: TextIO) -> None:
        self.source = source
        self.output = output
        self.lock = threading.Condition(threading.RLock())
        self.write_lock = threading.Lock()
        self.loop = asyncio.get_running_loop()
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self.operation: int | None = None
        self.turn_id: str | None = None
        self.interrupted = False
        self.provider_committed = False
        self.closing = False
        self.disconnected = False
        self.pending: dict[str, Any] | None = None
        self.choice: str | None = None
        self.worker: asyncio.Task[None] | None = None
        self.canceller: asyncio.Task[SettleOutcome] | None = None
        self.runtime: ProviderRuntime | None = None
        self.providers: ProviderSession | None = None
        self.session: Session | None = None
        self.permissions = SessionPermissionHandler(self.permission)
        self.env = dotenv_values(Path.cwd() / ".env")
        self.catalog: ProviderCatalog | None = None

    def send(self, message_type: str, **data: Any) -> None:
        if self.disconnected:
            return
        try:
            with self.write_lock:
                self.output.write(
                    json.dumps(
                        {"v": 1, "type": message_type, **data}, ensure_ascii=True
                    )
                    + "\n"
                )
                self.output.flush()
        except (BrokenPipeError, OSError):
            self.stop(disconnected=True)

    def error(self, request_id: int | None, code: str) -> None:
        # Never serialize exceptions, provider diagnostics or stderr.
        self.send("error", request_id=request_id, code=code)

    def stop(self, *, disconnected: bool = False) -> None:
        with self.lock:
            already_closing = self.closing
            self.disconnected |= disconnected
            self.closing = True
            self.interrupted = True
            self.lock.notify_all()
        self.loop.call_soon_threadsafe(self.cancel_worker)
        if not already_closing:
            self.loop.call_soon_threadsafe(self.enqueue, {"op": "shutdown", "id": None})

    def cancel_worker(self) -> None:
        if self.worker and not self.worker.done() and self.canceller is None:
            self.canceller = asyncio.create_task(settle_owned(self.worker))

    def enqueue(self, message: dict[str, Any]) -> None:
        try:
            self.inbox.put_nowait(message)
        except asyncio.QueueFull:
            self.stop(disconnected=True)

    def receive(self) -> None:
        last_id = 0
        try:
            while not self.closing:
                line = self.source.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    self.error(None, "message_too_large")
                    break
                try:
                    message = decode_request(line)
                    if message["id"] <= last_id:
                        raise ValueError("stale ID")
                    last_id = message["id"]
                except (ValueError, UnicodeError, RecursionError):
                    self.error(None, "invalid_request")
                    continue
                with self.lock:
                    op = message["op"]
                    if op == "approval":
                        pending = self.pending
                        if (
                            pending is None
                            or self.choice is not None
                            or self.interrupted
                            or time.monotonic() >= pending["deadline"]
                            or any(
                                message[k] != pending[k]
                                for k in ("approval_id", "request_id", "turn_id")
                            )
                        ):
                            self.error(message["id"], "stale_approval")
                        else:
                            self.choice = message["choice"]
                            self.lock.notify_all()
                            self.send("ack", request_id=message["id"])
                        continue
                    if op == "cancel":
                        if (
                            self.operation != message["request_id"]
                            or self.turn_id != message["turn_id"]
                            or self.provider_committed
                        ):
                            self.error(message["id"], "stale_cancel")
                            continue
                        self.interrupted = True
                        self.lock.notify_all()
                        self.loop.call_soon_threadsafe(self.cancel_worker)
                        self.send("ack", request_id=message["id"])
                        continue
                    if op == "shutdown":
                        self.stop()
                        return
                    if self.operation is not None:
                        self.error(message["id"], "busy")
                        continue
                    # Reserve admission in the reader so pipelined operations cannot
                    # race a blocking approval or bypass the busy check.
                    self.operation = message["id"]
                    self.interrupted = False
                    self.provider_committed = False
                    self.loop.call_soon_threadsafe(self.enqueue, message)
        except OSError:
            pass
        finally:
            self.stop(disconnected=not self.closing)

    def approve(self, kind: str, **details: Any) -> PermissionChoice:
        with self.lock:
            if self.interrupted or self.closing:
                raise asyncio.CancelledError
            pending: dict[str, Any] = {
                "approval_id": str(uuid4()),
                "request_id": self.operation,
                "turn_id": self.turn_id,
                "deadline": time.monotonic() + APPROVAL_SECONDS,
            }
            self.pending = pending
            self.choice = None
            self.send(
                "approval",
                **{k: v for k, v in pending.items() if k != "deadline"},
                kind=kind,
                **details,
            )
            try:
                while self.choice is None and not self.interrupted and not self.closing:
                    remaining = pending["deadline"] - time.monotonic()
                    if remaining <= 0:
                        break
                    self.lock.wait(remaining)
                if self.interrupted or self.closing:
                    raise asyncio.CancelledError
                return PermissionChoice(self.choice or "deny")
            finally:
                self.pending = None
                self.choice = None
                self.send("approval_closed", approval_id=pending["approval_id"])

    def authorize(self, tool_call: ToolCall) -> PermissionResult:
        with self.lock:
            if self.interrupted or self.closing:
                raise asyncio.CancelledError
            return self.permissions(tool_call)

    def permission(self, request: PermissionRequest) -> PermissionChoice:
        return self.approve(
            "permission",
            capability=request.capability.value,
            tool=request.tool_call.name,
            summary=str(request.tool_call.arguments.get("command", ""))[:300],
            justification=request.justification[:1000],
        )

    def confirm_provider(self, provider: NamedProvider) -> bool:
        return (
            self.approve(
                "provider",
                provider=provider.name,
                endpoint=provider.config.base_url,
                credential_variable=provider.config.api_key_env,
                models=[list(m.model_ids) for m in provider.config.model_config],
                notice="API key and full conversation will be sent to this endpoint. Successful switching resets tool grants.",
            )
            is PermissionChoice.ALLOW_ONCE
        )

    def mirror(self, runtime: ProviderRuntime) -> None:
        self.runtime = runtime

    @contextmanager
    def provider_commit(self) -> Iterator[None]:
        """Serialize the runtime owner's binding change with reader cancellation."""
        with self.lock:
            if self.interrupted or self.closing:
                raise asyncio.CancelledError
            yield
            self.provider_committed = True

    def status(self) -> None:
        assert self.runtime and self.catalog
        self.send(
            "ready",
            request_id=self.operation,
            provider=self.runtime.provider.name,
            model=self.runtime.model_manager.current_model().name,
            providers=[
                {"name": p.name, "models": [m.name for m in p.config.model_config]}
                for p in self.catalog.providers
            ],
        )

    def initialize(self, name: str | None = None) -> None:
        if self.session is not None:
            raise ValueError("already initialized")
        project = load_project_providers(Path(".cairn/models.toml"))
        provider = default_provider(project, os.environ, self.env)
        self.catalog = ProviderCatalog(project.providers or (provider,))
        self.send(
            "catalog",
            providers=[
                {"name": p.name, "models": [m.name for m in p.config.model_config]}
                for p in self.catalog.providers
            ],
        )
        if name is not None:
            provider = next(p for p in self.catalog.providers if p.name == name)
        if not self.confirm_provider(provider):
            raise ValueError("not approved")
        runtime = build_provider_runtime(
            provider,
            resolve_provider_api_key(provider.config, os.environ, self.env),
            resolve_context_budget(os.environ, self.env),
        )
        agent = build_agent(
            workspace=Workspace(Path.cwd()),
            llm=runtime.llm,
            event_handler=self.event,
            permission_handler=self.authorize,
            tracer=None,
            context_builder=runtime.context_builder,
            model_executor=runtime.model_executor,
            secret_env_keys=frozenset(
                p.config.api_key_env for p in self.catalog.providers
            ),
        )
        session = Session(agent)
        self.providers = ProviderSession(
            runtime,
            agent=agent,
            permissions=self.permissions,
            on_commit=self.mirror,
            commit_guard=self.provider_commit,
        )
        self.session = session

    def event(self, event: Event) -> None:
        if isinstance(event, SessionEvent):
            with self.lock:
                self.turn_id = str(event.turn_id)
            # Project only display fields; exception diagnostics never cross stdout.
            data = event.data
            if event.type == "tool_call":
                args = data.get("arguments", {})
                data = {
                    "tool": data.get("tool"),
                    "summary": str(args.get("command", args.get("path", "")))[:300],
                }
            elif event.type == "tool_result":
                data = {k: data.get(k) for k in ("tool", "exit_code")} | {
                    "output": str(data.get("stdout", ""))[:4000],
                    "truncated": bool(data.get("stdout_truncated"))
                    or len(str(data.get("stdout", ""))) > 4000,
                }
            elif event.type in {"tool_error", "tool_denied"}:
                data = {
                    "tool": data.get("tool"),
                    "status": "failed" if event.type == "tool_error" else "denied",
                }
            elif event.type not in {
                "turn_started",
                "turn_settled",
                "model_stream_start",
                "model_text_delta",
                "model_reasoning_delta",
                "model_stream_end",
                "agent_finish",
            }:
                return
            self.send(
                "event",
                request_id=self.operation,
                turn_id=self.turn_id,
                sequence=event.sequence,
                event=event.type,
                data=data,
            )

    async def execute(self, message: dict[str, Any]) -> None:
        try:
            op = message["op"]
            if self.interrupted:
                raise asyncio.CancelledError
            if op == "init" or (op == "provider" and self.session is None):
                self.initialize(message.get("name"))
            else:
                if not self.session or not self.providers or not self.catalog:
                    raise ValueError("not initialized")
                if op == "prompt":
                    await run_turn(
                        self.session.agent,
                        user_input=message["text"],
                        budget=DEFAULT_CLI_RUN_BUDGET,
                        session=self.session,
                    )
                elif op == "model":
                    self.providers.select_model(message["name"])
                elif op == "provider":
                    self.providers.select_provider(
                        message["name"],
                        self.catalog,
                        confirm=lambda p, m: self.confirm_provider(p),
                        resolve_credential=lambda config: resolve_provider_api_key(
                            config, os.environ, self.env
                        ),
                        context_budget=resolve_context_budget(os.environ, self.env),
                        commit_guard=self.provider_commit,
                    )
            if op != "prompt":
                self.status()
        except asyncio.CancelledError:
            self.send("cancelled", request_id=message["id"])
        except Exception:
            self.error(message["id"], "operation_failed")

    async def run(self) -> None:
        self.send(
            "hello",
            banner=files("cairn.resources").joinpath("banner.txt").read_text("utf-8"),
        )
        threading.Thread(target=self.receive, daemon=True, name="cairn-stdio").start()
        try:
            while not self.closing:
                message = await self.inbox.get()
                if message["op"] == "shutdown":
                    break
                self.worker = asyncio.create_task(self.execute(message))
                await await_owned(self.worker)
                if self.canceller:
                    await await_owned(self.canceller)
                if self.worker.cancelled():
                    self.send("cancelled", request_id=message["id"])
                # Admission belongs to the adapter, even if cancellation arrived
                # before execute entered its try/finally. Runtime owns the Turn.
                with self.lock:
                    self.operation = None
                    self.turn_id = None
                    self.canceller = None
                    self.send("idle", request_id=message["id"])
        finally:
            self.closing = True
            with self.lock:
                self.interrupted = True
                self.lock.notify_all()
            self.cancel_worker()
            if self.worker:
                await await_owned(self.worker)
            if self.canceller:
                await await_owned(self.canceller)
            if self.session:
                self.session.close()
            self.permissions.reset_grants()
            self.send("bye")


async def serve(source: BinaryIO, output: TextIO) -> None:
    bridge = Bridge(source, output)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda signum, frame: bridge.stop())
    await bridge.run()


def main() -> None:
    # Reserve the original fd for JSONL, including native/provider writes to fd 1.
    with os.fdopen(
        os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1
    ) as protocol:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        try:
            asyncio.run(serve(sys.stdin.buffer, protocol))
        except Exception:
            protocol.write(
                '{"v":1,"type":"error","request_id":null,"code":"backend_failed"}\n'
            )


if __name__ == "__main__":
    main()
