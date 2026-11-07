"""Real CLI -> shared Conversation -> Server HTTP acceptance."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from tests.support.git import initialize_repository
from tests.support.server_process import NativeClient, ServerProcess, wait_for

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux Server/PTY acceptance"
)


def test_real_cli_three_turn_conversation_and_web_history(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    terminal: NativeClient | None = None
    try:
        server.start()
        token_file = server.state / "runtime.token"
        token = token_file.read_text().strip()
        terminal = NativeClient(source, server.url, token_file)
        wait_for(lambda: b"Shared Runtime" in terminal.output)
        with httpx.Client(
            base_url=server.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
            timeout=5,
        ) as api:
            terminal.send("first turn")

            def first_completed() -> bool:
                rows = api.get("/api/runtime/conversations").json()
                return bool(
                    rows
                    and rows[0].get("latest_run")
                    and rows[0]["latest_run"]["status"] == "completed"
                )

            wait_for(first_completed, timeout=30)
            conversation_id = api.get("/api/runtime/conversations").json()[0][
                "conversation_id"
            ]
            terminal.send("second turn")
            wait_for(
                lambda: (
                    len(
                        api.get(f"/api/runtime/conversations/{conversation_id}").json()[
                            "runs"
                        ]
                    )
                    == 2
                    and api.get(f"/api/runtime/conversations/{conversation_id}").json()[
                        "runs"
                    ][-1]["status"]
                    == "completed"
                ),
                timeout=30,
            )
            terminal.send("third turn")
            wait_for(
                lambda: (
                    len(
                        api.get(f"/api/runtime/conversations/{conversation_id}").json()[
                            "runs"
                        ]
                    )
                    == 3
                    and api.get(f"/api/runtime/conversations/{conversation_id}").json()[
                        "runs"
                    ][-1]["status"]
                    == "completed"
                ),
                timeout=30,
            )
            history = api.get(
                f"/api/runtime/conversations/{conversation_id}/history"
            ).json()
            assert [item["content"] for item in history if item["role"] == "user"] == [
                "first turn",
                "second turn",
                "third turn",
            ]
            terminal.send("/exit")
            assert terminal.process.wait(timeout=10) == 0
            # The same conversation remains available after the CLI exits.
            detail = api.get(f"/api/runtime/conversations/{conversation_id}").json()
            assert len(detail["runs"]) == 3
    finally:
        if terminal is not None:
            terminal.close()
        server.stop()
