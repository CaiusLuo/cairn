"""Start an isolated Server and exercise the shipped bundle in Playwright."""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from tests.support.git import initialize_repository
from tests.support.server_process import ROOT, NativeClient, ServerProcess, wait_for


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="Playwright browser channel, e.g. chrome")
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="cairn-workbench-") as directory:
        root = Path(directory).resolve()
        source = root / "source"
        source.mkdir()
        initialize_repository(source)
        server = ServerProcess(source, root)
        try:
            server.start()
            token = server.state / "runtime.token"
            headers = {"Authorization": "Bearer " + token.read_text().strip()}
            with httpx.Client(
                base_url=server.url, headers=headers, trust_env=False, timeout=5
            ) as api:
                terminal = NativeClient(source, server.url, token)
                try:
                    wait_for(lambda: b"Shared Runtime" in terminal.output)
                    terminal.send("CLI browser takeover")

                    def cli_settled() -> bool:
                        return any(
                            item["initial_prompt"] == "CLI browser takeover"
                            and (item["latest_run"] or {}).get("status") == "completed"
                            for item in api.get("/api/runtime/conversations").json()
                        )

                    wait_for(cli_settled)
                    terminal.send("/exit")
                    terminal.process.wait(timeout=10)
                finally:
                    terminal.close()

                legacy = (
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": "Legacy browser history",
                            "source": "cli",
                            "request_id": "legacy-browser",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                api.post(
                    f"/api/runtime/conversations/{legacy['conversation_id']}/messages",
                    json={
                        "prompt": "Legacy browser history",
                        "source": "cli",
                        "request_id": "legacy-browser-message",
                    },
                ).raise_for_status()

                def legacy_settled() -> bool:
                    return any(
                        item["conversation_id"] == legacy["conversation_id"]
                        and (item["latest_run"] or {}).get("status") == "completed"
                        for item in api.get("/api/runtime/conversations").json()
                    )

                wait_for(legacy_settled)
                missing = (
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": "Missing session continuation",
                            "source": "cli",
                            "request_id": "missing-session",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                api.post(
                    f"/api/runtime/conversations/{missing['conversation_id']}/messages",
                    json={
                        "prompt": "Missing session continuation",
                        "source": "cli",
                        "request_id": "missing-session-message",
                    },
                ).raise_for_status()

                def missing_settled() -> bool:
                    return any(
                        item["conversation_id"] == missing["conversation_id"]
                        and (item["latest_run"] or {}).get("status") == "completed"
                        for item in api.get("/api/runtime/conversations").json()
                    )

                wait_for(missing_settled)
                # Enough conversations to exercise the latest-first limit and
                # the expand control in the conversation tree.
                for index in range(13):
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": f"Pagination fixture {index}",
                            "source": "cli",
                            "request_id": f"pagination-fixture-{index}",
                        },
                    ).raise_for_status()
                long_title = "Long conversation title " + "超长标题" * 40 + " END"
                long_conversation = (
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": long_title,
                            "source": "cli",
                            "request_id": "long-title",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                api.post(
                    f"/api/runtime/conversations/{long_conversation['conversation_id']}/messages",
                    json={
                        "prompt": long_title,
                        "source": "cli",
                        "request_id": "long-title-message",
                    },
                ).raise_for_status()

                def long_settled() -> bool:
                    return any(
                        item["conversation_id"] == long_conversation["conversation_id"]
                        and (item["latest_run"] or {}).get("status") == "completed"
                        for item in api.get("/api/runtime/conversations").json()
                    )

                wait_for(long_settled)
                rename_conversation = (
                    api.post(
                        "/api/runtime/conversations",
                        json={
                            "prompt": "Rename delete fixture",
                            "source": "cli",
                            "request_id": "rename-delete-fixture",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                # Reproduce the deployed shapes: old Runs predate Session
                # identity, and a newer Run points at a Session row that no
                # longer exists.
                server.stop()
                with sqlite3.connect(server.state / "domain.sqlite3") as database:
                    database.execute(
                        "UPDATE runs SET session_id = NULL WHERE conversation_id = ?",
                        (legacy["conversation_id"],),
                    )
                    database.execute(
                        "UPDATE runs SET session_id = ? WHERE conversation_id = ?",
                        (
                            "00000000-0000-4000-8000-000000000000",
                            missing["conversation_id"],
                        ),
                    )
                server.start()

                gate = server.gates / "after"
                gate.touch()
                task = (
                    api.post(
                        "/api/runtime/tasks",
                        json={
                            "prompt": "Interrupted browser recovery",
                            "source": "cli",
                            "mode": "development",
                            "request_id": "browser-recovery",
                        },
                    )
                    .raise_for_status()
                    .json()
                )
                wait_for(lambda: gate.with_suffix(".entered").exists())
                path = "/api/runtime/tasks/" + task["task_id"]
                running = api.get(path).raise_for_status().json()
                artifact = Path(running["worktree"]) / "runtime-result.txt"
                original = artifact.stat()
                server.stop(crash=True)
                gate.unlink()
                server.start()
                assert (
                    api.get(path).raise_for_status().json()["status"]
                    == "recovery_required"
                )
                environment = {
                    **os.environ,
                    "CAIRN_WEB_E2E_URL": server.url,
                    "CAIRN_WEB_E2E_TOKEN_FILE": str(token),
                    "CAIRN_WEB_E2E_LEGACY_CONVERSATION": legacy["conversation_id"],
                    "CAIRN_WEB_E2E_MISSING_SESSION_CONVERSATION": missing[
                        "conversation_id"
                    ],
                    "CAIRN_WEB_E2E_LONG_TITLE_CONVERSATION": long_conversation[
                        "conversation_id"
                    ],
                    "CAIRN_WEB_E2E_RENAME_CONVERSATION": rename_conversation[
                        "conversation_id"
                    ],
                    "CAIRN_WEB_E2E_RECOVERY_TASK": task["task_id"],
                    "CAIRN_WEB_E2E_REAL_MODEL": "0",
                }
                if args.channel:
                    environment["CAIRN_WEB_E2E_CHANNEL"] = args.channel
                if args.screenshots:
                    environment["CAIRN_WEB_E2E_SCREENSHOTS"] = str(
                        args.screenshots.resolve()
                    )
                subprocess.run(
                    ["pnpm", "--dir", "web", "test:e2e"],
                    cwd=ROOT,
                    env=environment,
                    check=True,
                    timeout=170,
                )
                server.stop()
                server.start()
                current = api.get(path).raise_for_status().json()
                assert current["status"] == "paused"
                assert (
                    current["recovery_note"] == "Reviewed the existing test artifact."
                )
                final = artifact.stat()
                assert (final.st_ino, final.st_mtime_ns) == (
                    original.st_ino,
                    original.st_mtime_ns,
                )
                clips = (
                    api.get("/api/knowledge/clips", params={"query": "追加行"})
                    .raise_for_status()
                    .json()["clips"]
                )
                assert len(clips) == 1
                assert clips[0]["content"].endswith("追加行")
                assert clips[0]["archived_at"] is None
                print("Recovery acknowledgement survived restart without replay.")
                print("Knowledge clip survived Server restart.")
        finally:
            server.stop()


if __name__ == "__main__":
    main()
