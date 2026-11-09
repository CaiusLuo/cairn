"""Detached supervisor for the WeChat owner: bounded restart and clean stop."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

_stopping = False


def _stop(_signum: int, _frame: object) -> None:
    global _stopping
    _stopping = True


def main() -> int:
    command = sys.argv[1:]
    if not command:
        print("supervisor: missing child command", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    backoff = 1.0
    while not _stopping:
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=sys.stdout,
                stderr=sys.stderr,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                close_fds=True,
            )
        except OSError as exc:
            print(
                f"supervisor: child launch failed: {exc}", file=sys.stderr, flush=True
            )
            time.sleep(backoff)
            backoff = min(60.0, backoff * 2)
            continue
        while True:
            if child.poll() is not None:
                break
            if _stopping:
                child.terminate()  # type: ignore[unreachable]
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                return 0
            time.sleep(0.2)
        if _stopping:
            return 0  # type: ignore[unreachable]
        print(
            f"supervisor: child exited {child.returncode}; restarting in {backoff:.0f}s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(backoff)
        backoff = min(60.0, backoff * 2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
