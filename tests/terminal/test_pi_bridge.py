"""End-to-end JSONL tests with an offline subprocess and the real turn owner."""

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import cairn.runtime.provider_runtime as provider_module
import cairn.terminal.bridge as bridge_module
from cairn.core.context import ContextBudget
from cairn.core.permissions import PermissionCapability
from cairn.core.provider_catalog import NamedProvider
from cairn.runtime.provider_runtime import ProviderRuntime
from tests.support.pi_backend import offline_runtime

ROOT = Path(__file__).resolve().parents[2]


class Client:
    def __init__(self, path: Path, *, fake: bool = True) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.support.pi_backend" if fake else "cairn.terminal.bridge",
            ],
            cwd=path,
            env={**os.environ, "PYTHONPATH": str(ROOT), "OFFLINE_KEY": "not-a-secret"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.id = 0
        self.seen: list[dict[str, Any]] = []
        threading.Thread(target=self.read, daemon=True).start()

    def read(self) -> None:
        assert self.process.stdout
        for line in self.process.stdout:
            self.messages.put(json.loads(line))

    def send(self, op: str, **fields: Any) -> int:
        self.id += 1
        assert self.process.stdin
        self.process.stdin.write(
            json.dumps({"v": 1, "id": self.id, "op": op, **fields}) + "\n"
        )
        self.process.stdin.flush()
        return self.id

    def until(self, kind: str, **fields: Any) -> dict[str, Any]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            msg = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            self.seen.append(msg)
            if msg["type"] == kind and all(msg.get(k) == v for k, v in fields.items()):
                return msg
        raise AssertionError(f"Missing {kind}")

    def approve(
        self, pending: dict[str, Any], choice: str = "allow_once", **overrides: Any
    ) -> None:
        self.send(
            "approval",
            **(
                {k: pending[k] for k in ("approval_id", "request_id", "turn_id")}
                | {"choice": choice}
                | overrides
            ),
        )

    def initialize(self) -> None:
        self.until("hello")
        self.send("init")
        self.approve(self.until("approval"))
        self.until("ready")
        self.until("idle")

    def close(self) -> None:
        if self.process.poll() is None:
            self.send("shutdown")
            self.process.wait(timeout=10)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe:
                pipe.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    (tmp_path / ".cairn").mkdir()
    (tmp_path / ".cairn/models.toml").write_text(
        "\n".join(
            f'[[providers]]\nname = "{provider}"\nbase_url = "https://offline.test/v1"\napi_key_env = "OFFLINE_KEY"\n'
            + "\n".join(
                f'[[providers.models]]\nname = "{model}"\nmodel_ids = ["offline/{provider}-{model}"]'
                for model in ("first", "second")
            )
            for provider in ("alpha", "beta", "broken")
        )
    )
    value = Client(tmp_path)
    try:
        value.initialize()
        yield value
    finally:
        value.close()


def test_subprocess_handshake_and_invalid_protocol(tmp_path: Path) -> None:
    value = Client(tmp_path, fake=False)
    try:
        assert "banner" in value.until("hello")
        value.send("unknown")
        assert value.until("error")["code"] == "invalid_request"
        value.send("shutdown")
        value.until("bye")
        assert value.process.wait(timeout=10) == 0
    finally:
        value.close()


def test_switch_changes_next_call_and_failed_provider_preserves_runtime(
    client: Client,
) -> None:
    client.send("model", name="second")
    assert client.until("ready")["model"] == "second"
    client.until("idle")
    client.send("prompt", text="hello")
    assert (
        "alpha-second" in client.until("event", event="agent_finish")["data"]["content"]
    )
    client.until("idle")
    for provider in ("beta", "broken"):
        client.send("provider", name=provider)
        client.approve(client.until("approval"))
        client.until("ready" if provider == "beta" else "error")
        client.until("idle")
    client.send("prompt", text="hello again")
    content = client.until("event", event="agent_finish")["data"]["content"]
    assert "beta-first" in content and "history=2" in content
    client.until("idle")


def test_stream_and_tool_events_are_ordered_and_bounded(client: Client) -> None:
    client.send("prompt", text="tool")
    client.until("idle")
    events = [m for m in client.seen if m["type"] == "event"]
    names = [e["event"] for e in events]
    assert names.index("model_reasoning_delta") < names.index("model_text_delta")
    assert (
        names.index("model_text_delta")
        < names.index("tool_call")
        < names.index("tool_result")
        < names.index("agent_finish")
        < names.index("turn_settled")
    )
    assert [e["sequence"] for e in events] == sorted({e["sequence"] for e in events})
    result = next(e["data"] for e in events if e["event"] == "tool_result")
    assert len(result["output"]) == 4000 and result["truncated"]


def test_denied_stale_and_cancelled_approval_cannot_execute(
    client: Client, tmp_path: Path
) -> None:
    client.send("prompt", text="permission")
    pending = client.until("approval")
    client.approve(pending, approval_id="stale")
    assert client.until("error")["code"] == "stale_approval"
    client.approve(pending, "deny")
    client.until("idle")
    assert not (tmp_path / "executed").exists()
    client.send("prompt", text="permission")
    pending = client.until("approval")
    client.send("cancel", request_id=pending["request_id"], turn_id=pending["turn_id"])
    client.approve(pending, "allow_session")
    client.until("idle")
    assert not (tmp_path / "executed").exists()
    client.send("prompt", text="permission")
    pending = client.until("approval")
    client.approve(pending, "allow_session")
    client.until("idle")
    assert (tmp_path / "executed").read_text() == "authorized"


def test_repeated_cancel_busy_and_eof_wait_for_tool_cleanup(
    client: Client, tmp_path: Path
) -> None:
    request_id = client.send("prompt", text="slow")
    event = client.until("event", event="tool_call")
    deadline = time.monotonic() + 5
    while not (tmp_path / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (tmp_path / "started").exists()
    client.send("model", name="second")
    assert client.until("error")["code"] == "busy"
    for _ in range(3):
        client.send("cancel", request_id=request_id, turn_id=event["turn_id"])
    client.until("idle")
    assert (tmp_path / "cleaned").exists()
    (tmp_path / "started").unlink()
    (tmp_path / "cleaned").unlink()
    client.send("prompt", text="slow")
    client.until("event", event="tool_call")
    deadline = time.monotonic() + 5
    while not (tmp_path / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert client.process.stdin
    client.process.stdin.close()
    assert client.process.wait(timeout=10) == 0
    assert (tmp_path / "cleaned").exists()


def test_backend_errors_do_not_expose_provider_secrets(client: Client) -> None:
    client.send("prompt", text="fail")
    assert client.until("error")["code"] == "operation_failed"
    client.until("idle")
    assert "SECRET-MUST-NOT-LEAK" not in json.dumps(client.seen)


@pytest.mark.parametrize("initial", [True, False], ids=["activation", "switch"])
def test_provider_commit_is_linearized_with_reader_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFFLINE_KEY", "offline")
    (tmp_path / ".cairn").mkdir()
    (tmp_path / ".cairn/models.toml").write_text(
        "\n".join(
            f'[[providers]]\nname = "{name}"\nbase_url = "https://offline.test/v1"\n'
            'api_key_env = "OFFLINE_KEY"\n[[providers.models]]\n'
            f'name = "first"\nmodel_ids = ["offline/{name}"]\n'
            for name in ("alpha", "beta")
        )
    )
    target = "alpha" if initial else "beta"
    building = threading.Event()
    release_build = threading.Event()
    committed = threading.Event()
    release_status = threading.Event()
    messages: queue.Queue[dict[str, Any]] = queue.Queue()
    bridges: queue.Queue[bridge_module.Bridge] = queue.Queue()
    failures: queue.Queue[BaseException] = queue.Queue()
    seen: list[dict[str, Any]] = []
    block_status = False

    def build(
        provider: NamedProvider,
        credential: str,
        context_budget: ContextBudget,
        *,
        group: str | None = None,
    ) -> ProviderRuntime:
        candidate = offline_runtime(provider, credential, context_budget, group=group)
        if provider.name == target:
            building.set()
            assert release_build.wait(5), "provider builder was not released"
        return candidate

    original_status = bridge_module.Bridge.status

    def status(bridge: bridge_module.Bridge) -> None:
        if block_status:
            committed.set()
            assert release_status.wait(5), "committed status was not released"
        original_status(bridge)

    class Output(StringIO):
        def write(self, text: str) -> int:
            messages.put(json.loads(text))
            return len(text)

    monkeypatch.setattr(bridge_module, "build_provider_runtime", build)
    monkeypatch.setattr(provider_module, "build_provider_runtime", build)
    monkeypatch.setattr(bridge_module.Bridge, "status", status)
    monkeypatch.setattr(bridge_module.Bridge, "confirm_provider", lambda self, p: True)
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, "rb")
    writer = os.fdopen(write_fd, "w")

    async def run() -> None:
        bridge = bridge_module.Bridge(source, Output())
        bridges.put(bridge)
        await bridge.run()

    def run_backend() -> None:
        try:
            asyncio.run(run())
        except BaseException as exc:
            failures.put(exc)

    backend = threading.Thread(target=run_backend, daemon=True)
    backend.start()
    bridge = bridges.get(timeout=5)
    request_id = 0

    def send(op: str, **fields: Any) -> int:
        nonlocal request_id
        request_id += 1
        writer.write(json.dumps({"v": 1, "id": request_id, "op": op, **fields}) + "\n")
        writer.flush()
        return request_id

    def until(kind: str, **fields: Any) -> dict[str, Any]:
        while True:
            message = messages.get(timeout=5)
            seen.append(message)
            if message["type"] == kind and all(
                message.get(k) == v for k, v in fields.items()
            ):
                return message

    try:
        until("hello")
        if not initial:
            until("idle", request_id=send("init"))
            until("idle", request_id=send("prompt", text="keep this conversation"))
        with bridge.lock:
            previous = bridge.runtime
            providers = bridge.providers
            session = bridge.session
            history = session.agent.state.messages.copy() if session else []
            bridge.permissions.grants.add(PermissionCapability.NETWORK)

        operation = send("init") if initial else send("provider", name=target)
        assert building.wait(5)
        cancellation = send("cancel", request_id=operation, turn_id=None)
        # Receiving the ack proves cancellation was accepted while the builder
        # was blocked, without sleeping or relying on scheduler timing.
        until("ack", request_id=cancellation)
        release_build.set()
        until("idle", request_id=operation)
        assert bridge.runtime is previous
        assert bridge.providers is providers
        assert bridge.session is session
        assert bridge.permissions.grants == {PermissionCapability.NETWORK}
        if session:
            assert session.agent.state.messages == history
            assert previous is not None
            assert session.agent.llm is previous.llm
            assert session.agent.context_builder is previous.context_builder
            assert session.agent.model_executor is previous.model_executor
        replies = [m["type"] for m in seen if m.get("request_id") == operation]
        assert "cancelled" in replies and "ready" not in replies

        # Hold the operation open after commitment, before ready is sent. A
        # cancellation here must not claim the old provider remained active.
        block_status = True
        operation = send("init") if initial else send("provider", name=target)
        assert committed.wait(5)
        cancellation = send("cancel", request_id=operation, turn_id=None)
        assert until("error", request_id=cancellation)["code"] == "stale_cancel"
        release_status.set()
        assert until("ready", request_id=operation)["provider"] == target
        until("idle", request_id=operation)
        assert bridge.runtime is not previous
        assert bridge.session is not None
        if session:
            assert bridge.session is session
            assert session.agent.state.messages == history
            assert not bridge.permissions.grants
        assert not any(
            m["type"] == "cancelled" and m.get("request_id") == operation for m in seen
        )
    finally:
        release_build.set()
        release_status.set()
        send("shutdown")
        backend.join(timeout=5)
        writer.close()
        source.close()
        assert not backend.is_alive(), "backend did not settle"
        assert failures.empty()
