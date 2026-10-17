import asyncio
import multiprocessing
import os
import shlex
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from cairn.core.memory.models import Candidate, Lifecycle, MemoryError, VersionConflict
from cairn.infra.memory.identity import ProjectIdentity
from cairn.infra.memory.store import MemoryStore
from cairn.infra.tools.bash import BashTool
from cairn.infra.tools.files import EditFileTool
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.memory.service import (
    ApprovalAuthority,
    ApprovalRequest,
    MemoryService,
)
from tests.support.git import git
from tests.support.memory import (
    PROJECT,
    accept_synthetic,
    attestation,
    candidate,
    replace_synthetic,
    repository,
    service,
)
from tests.support.sandbox import require_working_sandbox


#: A qualified runtime for this suite. MemoryStore now refuses an unqualified
#: SQLite before any effect (ADR 0002), so these store-level tests pin the
#: predicate instead of depending on whichever SQLite the host happens to link.
def _pin_worker_version() -> None:
    """Present the qualified version in a spawned worker (see the shared helper)."""
    from tests.support.sqlite_runtime import pin_worker_runtime

    pin_worker_runtime()


def _propose(root: str, number: int) -> str:
    _pin_worker_version()
    return (
        service(Path(root))
        .propose(candidate(f"Contract number {number} requires parser validation."))
        .id
    )


def _race_accept(root: str, entry_id: str) -> bool:
    _pin_worker_version()
    api = service(Path(root))
    try:
        accept_synthetic(api, entry_id, 1, attestation(api.inspect(entry_id).candidate))
        return True
    except VersionConflict:
        return False


def test_lifecycle_idempotence_replace_delete_and_reopen(tmp_path: Path) -> None:
    api = service(tmp_path)
    proposed = api.propose(candidate())
    assert api.propose(candidate()) == proposed
    accepted = accept_synthetic(api, proposed.id, 1, attestation(proposed.candidate))
    assert accepted.lifecycle is Lifecycle.ACCEPTED
    newer = api.propose(
        candidate("Run parser validation before changing its contract.")
    )
    replacement = replace_synthetic(
        api, accepted.id, 2, newer.id, 1, attestation(newer.candidate)
    )
    assert replacement.supersedes_id == accepted.id
    assert api.inspect(accepted.id).lifecycle is Lifecycle.SUPERSEDED
    deleted = api.delete(newer.id, 2)
    reopened = service(tmp_path)
    assert reopened.inspect(newer.id) == deleted
    assert reopened.propose(newer.candidate).lifecycle is Lifecycle.DELETED
    with pytest.raises(MemoryError, match="lifecycle"):
        accept_synthetic(reopened, newer.id, 3, attestation(newer.candidate))


def test_approval_is_host_callback_not_model_boolean(tmp_path: Path) -> None:
    api = service(tmp_path, approved=False)
    entry = api.propose(candidate())
    with pytest.raises(MemoryError, match="approval denied"):
        accept_synthetic(api, entry.id, 1, attestation(entry.candidate))
    with pytest.raises(MemoryError, match="approval denied"):
        api.delete(entry.id, 1)
    assert api.inspect(entry.id).version == 1
    with pytest.raises(ValidationError):
        Candidate.model_validate({**candidate().model_dump(), "approved": True})
    requests: list[ApprovalRequest] = []

    def decide(request: ApprovalRequest) -> bool:
        requests.append(request)
        return True

    approving = MemoryService(api.store, ApprovalAuthority(decide))
    accept_synthetic(approving, entry.id, 1, attestation(entry.candidate))
    assert requests[0].entry.candidate == entry.candidate
    assert requests[0].attestation == attestation(entry.candidate)


def test_concurrent_subprocesses_and_compare_and_swap(tmp_path: Path) -> None:
    api = service(tmp_path)  # Initialize before unrelated worker processes.
    with ProcessPoolExecutor(
        max_workers=3, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        ids = list(pool.map(_propose, [str(tmp_path)] * 6, range(6)))
        won = list(pool.map(_race_accept, [str(tmp_path)] * 3, [ids[0]] * 3))
    assert len(set(ids)) == 6
    assert won.count(True) == 1
    assert len(api.store.entries()) == 6
    assert api.inspect(ids[0]).version == 2


def test_transaction_failure_rolls_back_all_rows(tmp_path: Path) -> None:
    api = service(tmp_path)
    first = api.propose(candidate())
    second = api.propose(candidate("Different scoped rule."))
    updates = tuple(
        (e.model_copy(update={"version": e.version + 1}), 999 if e == second else 1)
        for e in (first, second)
    )
    with pytest.raises(VersionConflict):
        api.store.update_entries(updates)
    assert api.inspect(first.id).version == 1
    with pytest.raises(RuntimeError), api.store._connect(write=True) as conn:
        conn.execute("DELETE FROM entries")
        raise RuntimeError("simulated interruption")
    assert len(api.store.entries()) == 2


@pytest.mark.parametrize("damage", ["schema", "corrupt", "project"])
def test_no_silent_reset(tmp_path: Path, damage: str) -> None:
    api = service(tmp_path)
    api.propose(candidate())
    if damage == "corrupt":
        api.store.path.write_bytes(b"not a database")
    else:
        with sqlite3.connect(api.store.path) as conn:
            conn.execute(
                "PRAGMA user_version=99"
                if damage == "schema"
                else "UPDATE metadata SET project_id='wrong'"
            )
    before = api.store.path.read_bytes()
    with pytest.raises(MemoryError):
        service(tmp_path)
    assert api.store.path.read_bytes() == before


@pytest.mark.parametrize(
    "text",
    [
        "password=sentinel-password",
        "sk-abcdefghijklmno123456",
        "https://user:pass@example.com",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_sensitive_content_never_reaches_database(tmp_path: Path, text: str) -> None:
    api = service(tmp_path)
    with pytest.raises(MemoryError) as error:
        api.propose(candidate(text))
    assert text not in str(error.value)
    assert not api.store.entries()
    for file in api.store.directory.iterdir():
        assert text.encode() not in file.read_bytes()


def test_known_secret_and_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore(
        tmp_path / "storage",
        PROJECT,
        forbidden_roots=(tmp_path / "work",),
        sensitive_values=("opaque-sensitive-value",),
    )
    api = MemoryService(store, ApprovalAuthority(lambda _: True))
    with pytest.raises(MemoryError, match="sensitive"):
        api.propose(candidate("Remember opaque-sensitive-value"))
    with pytest.raises(ValidationError):
        candidate("x" * 1201)
    monkeypatch.setattr("cairn.infra.memory.store.MAX_ENTRIES", 1)
    api.propose(candidate())
    with pytest.raises(MemoryError, match="limit"):
        api.propose(candidate("Other rule"))


def test_storage_overlap_and_symlinks_rejected(tmp_path: Path) -> None:
    with pytest.raises(MemoryError, match="overlaps"):
        MemoryStore(tmp_path, PROJECT, forbidden_roots=(tmp_path,))
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    with pytest.raises(MemoryError, match="symlink"):
        MemoryStore(alias, PROJECT, forbidden_roots=(tmp_path / "work",))


def test_project_identity_worktrees_clones_nested_workspace(tmp_path: Path) -> None:
    root = repository(tmp_path / "repo")
    other = tmp_path / "other"
    clone = tmp_path / "clone"
    git(root, "worktree", "add", "-b", "feature", str(other))
    git(root, "clone", str(root), str(clone))
    nested = root / "nested"
    nested.mkdir()
    identities = [
        asyncio.run(ProjectIdentity.inspect(Workspace(p)))
        for p in (root, other, clone, nested)
    ]
    assert (
        identities[0].project_id == identities[1].project_id == identities[3].project_id
    )
    assert identities[0].project_id != identities[2].project_id
    assert identities[3].workspace_prefix == "nested/"
    with pytest.raises(MemoryError):
        asyncio.run(ProjectIdentity.inspect(Workspace(tmp_path)))


def _crash_transaction(path: str) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM entries")
    os._exit(17)


def test_process_crash_preserves_committed_rows(tmp_path: Path) -> None:
    api = service(tmp_path)
    entry = api.propose(candidate())
    worker = multiprocessing.get_context("spawn").Process(
        target=_crash_transaction, args=(str(api.store.path),)
    )
    worker.start()
    worker.join(timeout=10)
    assert worker.exitcode == 17
    assert service(tmp_path).inspect(entry.id) == entry


def test_normal_tools_cannot_write_external_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("TMPDIR", str(work))
    monkeypatch.setenv("UV_CACHE_DIR", str(work / "uv-cache"))
    workspace = Workspace(work)
    require_working_sandbox(workspace)
    api = service(tmp_path)
    api.propose(candidate())
    before = api.store.path.read_bytes()
    result = asyncio.run(
        BashTool(workspace).execute(
            {"command": "printf changed > " + shlex.quote(str(api.store.path))}
        )
    )
    assert result.exit_code != 0
    with pytest.raises(ValueError):
        asyncio.run(
            EditFileTool(workspace).execute(
                {"path": str(api.store.path), "old_text": "", "new_text": "changed"}
            )
        )
    assert api.store.path.read_bytes() == before
