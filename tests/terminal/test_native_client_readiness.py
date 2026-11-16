"""The PTY driver must wait for raw input mode, not an early startup banner."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from tests.support.server_process import NativeClient, wait_for


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PTY acceptance")
def test_send_waits_for_terminal_input_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "cairn-cli"
    gate = tmp_path / "enable-input"
    received = tmp_path / "received"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, time, tty\nfrom pathlib import Path\n"
        "print('early banner', flush=True)\n"
        f"while not Path({str(gate)!r}).exists(): time.sleep(0.01)\n"
        # TCSAFLUSH discards input delivered before the terminal is ready.
        "tty.setraw(0)\n"
        f"Path({str(received)!r}).write_bytes(os.read(0, 1024))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
    terminal = NativeClient(tmp_path, "unused", tmp_path / "token")
    completed = threading.Event()
    errors: list[BaseException] = []

    def send() -> None:
        try:
            terminal.send("hello")
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    sender = threading.Thread(target=send)
    try:
        wait_for(lambda: b"early banner" in terminal.output)
        sender.start()
        assert not completed.wait(0.2), "input was sent before raw mode was enabled"
        gate.touch()
        assert completed.wait(5)
        assert not errors
        assert terminal.process.wait(timeout=5) == 0
        assert received.read_bytes() == b"hello\r"
    finally:
        gate.touch()
        if sender.ident is not None:
            sender.join(timeout=5)
        terminal.close()
