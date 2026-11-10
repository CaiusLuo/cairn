"""Real Server process fault injection for Conversation Run/Session identity."""

from __future__ import annotations

from pathlib import Path

import httpx

from tests.support.git import initialize_repository
from tests.support.server_process import ServerProcess, wait_for


def test_run_session_is_persisted_before_crash_and_can_be_recovered(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(source, tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        gate = server.gates / "before"
        gate.touch()
        with httpx.Client(
            base_url=server.url, headers=headers, trust_env=False, timeout=10
        ) as api:
            conversation = (
                api.post(
                    "/api/runtime/conversations",
                    json={"prompt": "crash recovery", "source": "web"},
                )
                .raise_for_status()
                .json()
            )
            run = (
                api.post(
                    f"/api/runtime/conversations/{conversation['conversation_id']}/messages",
                    json={
                        "prompt": "wait in the model",
                        "source": "web",
                        "request_id": "session-binding-crash",
                    },
                )
                .raise_for_status()
                .json()
            )
            wait_for(lambda: (server.gates / "before.entered").exists(), timeout=10)
            current = (
                api.get(f"/api/runtime/runs/{run['run_id']}").raise_for_status().json()
            )
            assert current["status"] == "running", current
            assert current["session_id"], current

            # Kill the process while the first model call is still blocked.
            server.stop(crash=True)
            gate.unlink(missing_ok=True)
            server.start()
            token = (server.state / "runtime.token").read_text().strip()
            headers = {"Authorization": "Bearer " + token}

        with httpx.Client(
            base_url=server.url, headers=headers, trust_env=False, timeout=10
        ) as api:
            recovered = (
                api.get(f"/api/runtime/runs/{run['run_id']}").raise_for_status().json()
            )
            assert recovered["status"] == "recovery_required", recovered
            assert recovered["session_id"] == current["session_id"]

            report = (
                api.get(f"/api/runtime/runs/{run['run_id']}/recovery")
                .raise_for_status()
                .json()
            )
            assert report["session_id"] == current["session_id"]
            assert report["run_id"] == run["run_id"]

            reconciled = (
                api.post(
                    f"/api/runtime/runs/{run['run_id']}/reconcile",
                    json={
                        "revision": report["revision"],
                        "action": report["action"],
                        "reviewed_effects": True,
                        "note": "Inspected the interrupted turn; no tool effects were observed.",
                    },
                )
                .raise_for_status()
                .json()
            )
            assert reconciled["status"] == "paused", reconciled

            next_run = (
                api.post(
                    f"/api/runtime/conversations/{conversation['conversation_id']}/messages",
                    json={
                        "prompt": "continue after recovery",
                        "source": "web",
                        "request_id": "session-binding-continue",
                    },
                )
                .raise_for_status()
                .json()
            )
            wait_for(
                lambda: (
                    api.get(f"/api/runtime/runs/{next_run['run_id']}")
                    .raise_for_status()
                    .json()["status"]
                    in {
                        "completed",
                        "paused",
                        "failed",
                        "cancelled",
                        "recovery_required",
                    }
                ),
                timeout=20,
            )
            final = (
                api.get(f"/api/runtime/runs/{next_run['run_id']}")
                .raise_for_status()
                .json()
            )
            assert final["status"] == "completed", final
    finally:
        server.stop()


def test_tool_boundary_crash_preserves_session_binding_and_recovery(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    server = ServerProcess(
        source,
        tmp_path,
        extra_env={"CAIRN_TEST_CHAT_TOOL": "1"},
    )
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        gate = server.gates / "after"
        gate.touch()
        with httpx.Client(
            base_url=server.url, headers=headers, trust_env=False, timeout=10
        ) as api:
            conversation = (
                api.post(
                    "/api/runtime/conversations",
                    json={"prompt": "tool crash", "source": "web"},
                )
                .raise_for_status()
                .json()
            )
            run = (
                api.post(
                    f"/api/runtime/conversations/{conversation['conversation_id']}/messages",
                    json={
                        "prompt": "read the tracked file",
                        "source": "web",
                        "request_id": "tool-boundary-crash",
                    },
                )
                .raise_for_status()
                .json()
            )
            wait_for(lambda: (server.gates / "after.entered").exists(), timeout=10)
            current = (
                api.get(f"/api/runtime/runs/{run['run_id']}").raise_for_status().json()
            )
            assert current["status"] == "running", current
            assert current["session_id"], current

            server.stop(crash=True)
            gate.unlink(missing_ok=True)
            server.start()
            token = (server.state / "runtime.token").read_text().strip()
            headers = {"Authorization": "Bearer " + token}

        with httpx.Client(
            base_url=server.url, headers=headers, trust_env=False, timeout=10
        ) as api:
            recovered = (
                api.get(f"/api/runtime/runs/{run['run_id']}").raise_for_status().json()
            )
            assert recovered["status"] == "recovery_required", recovered
            assert recovered["session_id"] == current["session_id"]
            report = (
                api.get(f"/api/runtime/runs/{run['run_id']}/recovery")
                .raise_for_status()
                .json()
            )
            assert report["session_id"] == current["session_id"]
            reconciled = (
                api.post(
                    f"/api/runtime/runs/{run['run_id']}/reconcile",
                    json={
                        "revision": report["revision"],
                        "action": report["action"],
                        "reviewed_effects": True,
                        "note": "Reviewed the recorded read-only tool call; no writes occurred.",
                    },
                )
                .raise_for_status()
                .json()
            )
            assert reconciled["status"] == "paused", reconciled
            next_run = (
                api.post(
                    f"/api/runtime/conversations/{conversation['conversation_id']}/messages",
                    json={
                        "prompt": "continue after tool recovery",
                        "source": "web",
                        "request_id": "tool-boundary-continue",
                    },
                )
                .raise_for_status()
                .json()
            )
            wait_for(
                lambda: (
                    api.get(f"/api/runtime/runs/{next_run['run_id']}")
                    .raise_for_status()
                    .json()["status"]
                    in {
                        "completed",
                        "paused",
                        "failed",
                        "cancelled",
                        "recovery_required",
                    }
                ),
                timeout=20,
            )
            final = (
                api.get(f"/api/runtime/runs/{next_run['run_id']}")
                .raise_for_status()
                .json()
            )
            assert final["status"] in {"completed", "paused"}, final
    finally:
        server.stop()
