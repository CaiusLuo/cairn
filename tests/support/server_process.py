"""Bounded real-process/PTY helpers for Server lifecycle acceptance."""

from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(predicate: Callable[[], Any], timeout: float = 30) -> Any:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.04)
    raise AssertionError("Timed out waiting for Server acceptance condition")


class NativeClient:
    def __init__(
        self, cwd: Path, url: str, token: Path, task_id: str | None = None
    ) -> None:
        import fcntl
        import pty
        import struct
        import termios

        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
        command = [
            str(Path(sys.executable).parent / "cairn-cli"),
            "--runtime-url",
            url,
            "--runtime-token-file",
            str(token),
        ]
        if task_id:
            command.extend(["--task-id", task_id])
        try:
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env={**os.environ, "TERM": "xterm-256color"},
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.output = bytearray()
        self.stopped = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        while not self.stopped.is_set():
            try:
                if not select.select([self.master], [], [], 0.05)[0]:
                    continue
                data = os.read(self.master, 8192)
                if not data:
                    return
                self.output.extend(data)
                if len(self.output) > 1024 * 1024:
                    del self.output[: len(self.output) - 1024 * 1024]
                if b"\x1b[6n" in data:
                    os.write(self.master, b"\x1b[1;1R")
            except OSError:
                return

    def send(self, text: str) -> None:
        os.write(self.master, (text + "\r").encode())

    def close(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.stopped.set()
        self.reader.join(timeout=2)
        os.close(self.master)


class ServerProcess:
    def __init__(self, source: Path, root: Path) -> None:
        self.source, self.root = source, root
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.state = root / "state"
        self.gates = root / "gates"
        self.gates.mkdir()
        self.log = root / "server.log"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        import httpx

        with self.log.open("ab") as log:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tests.support.runtime_server",
                    "--workspace",
                    str(self.source),
                    "--state-dir",
                    str(self.state),
                    "--worktrees",
                    str(self.root / "worktrees"),
                    "--offline",
                    "--port",
                    str(self.port),
                ],
                cwd=ROOT,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env={**os.environ, "CAIRN_TEST_GATES": str(self.gates)},
            )

        def ready() -> bool:
            assert self.process is not None
            assert self.process.poll() is None, self.log.read_text()[-4000:]
            try:
                return (
                    httpx.get(
                        self.url + "/api/health", timeout=0.5, trust_env=False
                    ).status_code
                    == 200
                )
            except httpx.TransportError:
                return False

        wait_for(ready)

    def stop(self, *, crash: bool = False) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        os.killpg(self.process.pid, signal.SIGKILL if crash else signal.SIGTERM)
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
            raise
