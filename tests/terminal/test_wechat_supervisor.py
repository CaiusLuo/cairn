import subprocess
import sys
import time
from pathlib import Path


def test_supervisor_restarts_failed_child_then_stops(tmp_path: Path) -> None:
    log = tmp_path / "supervisor.log"
    with log.open("wb") as handle:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from cairn.terminal.wechat.supervisor import main; main()",
                sys.executable,
                "-c",
                "import sys; sys.exit(3)",
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        try:
            time.sleep(1.5)
            process.terminate()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    text = log.read_text(errors="replace")
    assert "restarting" in text
