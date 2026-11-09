import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.agent.workspace.lease import WorkspaceLease
from cairn.server.paths import ServerPaths
from cairn.server.state_import import import_state
from tests.server.test_bootstrap import configured
from tests.support.server_process import ServerProcess, wait_for


def layout(tmp_path: Path) -> tuple[ServerPaths, Path, Path]:
    source = tmp_path / "workspace"
    source.mkdir()
    return (
        ServerPaths(source, tmp_path / "new", tmp_path / "trees"),
        tmp_path / "old",
        tmp_path / "backup",
    )


def test_failed_import_retains_original_and_verified_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, old, backups = layout(tmp_path)
    (old / "agent").mkdir(parents=True)
    (old / "wechat").mkdir()
    (old / "agent/MEMORY.md").write_text("retained memory")
    (old / "wechat/account.json").write_text('{"private": "test-credential"}')
    rename = os.rename
    count = 0

    def interrupted(source: Any, destination: Any) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected publish failure")
        rename(source, destination)

    monkeypatch.setattr(os, "rename", interrupted)
    with pytest.raises(OSError, match="injected"):
        import_state(paths, old, backups, components=("agent", "wechat"))
    assert (old / "agent/MEMORY.md").read_text() == "retained memory"
    backup = next(backups.iterdir())
    assert (backup / "manifest.json").is_file()
    assert (
        backup / "wechat/account.json"
    ).read_text() == '{"private": "test-credential"}'
    assert not paths.agent.exists() and not paths.wechat.exists()


def test_live_legacy_owner_is_never_stopped_or_imported(tmp_path: Path) -> None:
    paths, old, backups = layout(tmp_path)
    lease = WorkspaceLease(old / "runtime/owner.lock")
    assert lease.acquire()
    try:
        with pytest.raises(RuntimeError, match="State is in use"):
            import_state(paths, old, backups)
        assert lease._handle is not None
        assert not backups.exists()
    finally:
        lease.release()


def test_import_refuses_corrupt_or_occupied_state(tmp_path: Path) -> None:
    paths, old, backups = layout(tmp_path)
    (old / "runtime").mkdir(parents=True)
    (old / "runtime/runtime.json").write_text("damaged")
    with pytest.raises(ValueError, match="Invalid JSON"):
        import_state(paths, old, backups)
    assert (old / "runtime/runtime.json").read_text() == "damaged"
    paths.runtime.mkdir(parents=True, exist_ok=True)
    (paths.runtime / "keep.json").write_text("{}")
    with pytest.raises(ValueError, match="not empty"):
        import_state(paths, old, backups)
    assert (paths.runtime / "keep.json").read_text() == "{}"


def test_imported_server_preserves_session_history_and_worktree_identity(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    old = configured(legacy_root)
    new_root = tmp_path / "new-host"
    new_root.mkdir()
    new = ServerProcess(old.source, new_root)
    new.config.write_text(
        new.config.read_text().replace(
            str(new_root / "worktrees"), str(legacy_root / "worktrees")
        )
    )
    try:
        old.start()
        token = (old.state / "runtime.token").read_text().strip()
        with httpx.Client(
            base_url=old.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as api:
            response = api.post(
                "/api/runtime/tasks",
                json={
                    "prompt": "Write the migration acceptance artifact",
                    "source": "web",
                    "mode": "development",
                },
            )
            assert response.status_code == 200, response.text
            task_id = response.json()["task_id"]
            task = wait_for(
                lambda: (
                    value
                    if (value := api.get("/api/runtime/tasks/" + task_id).json())[
                        "status"
                    ]
                    == "completed"
                    else None
                )
            )
            history = api.get("/api/runtime/tasks/" + task_id + "/history").json()
        old.stop()
        artifact = Path(task["worktree"]) / "runtime-result.txt"
        original_identity = artifact.stat().st_ino
        paths = ServerPaths(old.source, new.data_root, legacy_root / "worktrees")
        backup = import_state(paths, old.data_root, tmp_path / "backups")
        manifest = json.loads((backup / "manifest.json").read_text())
        for relative, expected in manifest.items():
            assert (
                hashlib.sha256((new.data_root / relative).read_bytes()).hexdigest()
                == expected
            )
        new.start()
        with httpx.Client(
            base_url=new.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as api:
            restored = api.get("/api/runtime/tasks/" + task_id).json()
            assert (
                restored["session_id"],
                restored["worktree"],
                restored["status"],
            ) == (task["session_id"], task["worktree"], "completed")
            assert (
                api.get("/api/runtime/tasks/" + task_id + "/history").json() == history
            )
        assert artifact.stat().st_ino == original_identity
        assert (old.state / "sessions/sessions.sqlite3").is_file()
    finally:
        old.stop()
        new.stop()


def test_component_root_symlink_is_rejected_without_touching_workspace(
    tmp_path: Path,
) -> None:
    paths, old, backups = layout(tmp_path)
    old.mkdir()
    (old / "agent").symlink_to(paths.workspace, target_is_directory=True)
    (paths.workspace / "MEMORY.md").write_text("workspace is not host state")
    with pytest.raises(ValueError, match="symlink"):
        import_state(paths, old, backups, components=("agent",))
    assert sorted(p.name for p in paths.workspace.iterdir()) == ["MEMORY.md"]
    assert not backups.exists()
