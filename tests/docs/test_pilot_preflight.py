"""Keep the pilot preflight's verdict honest.

The preflight is the operator's only offline admission gate, so its two rules
must hold: a refused SQLite runtime blocks the durable profile but never the
stateless one, and a declared-unverified runtime is never reported as supported.
These tests drive the real check functions; they do not reimplement them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from scripts.pilot import preflight

from cairn.infra.sessions.store import UNVERIFIED_SQLITE_DECLARATION

ROOT = Path(preflight.__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clear_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UNVERIFIED_SQLITE_DECLARATION, raising=False)


def _pin_sqlite_version(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
) -> None:
    """Describe a simulated runtime through the single seam the preflight reads.

    check_sqlite feeds one version to both sqlite_wal_fix_supported() and
    wal_admission(), so pinning the version keeps the two decisions consistent.
    Stubbing only one of them let the real wal_admission() see the host's
    interpreter and disagree with the stubbed support verdict.
    """
    monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
    monkeypatch.setattr(
        sqlite3, "sqlite_version", ".".join(str(part) for part in version)
    )


def _status(checks: list[preflight.Check], name: str) -> str:
    for check in checks:
        if check.name == name:
            return check.status
    raise AssertionError(f"{name} was not reported; got {[c.name for c in checks]}")


def test_stateless_profile_does_not_require_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stateless mode never opens a store, so the runtime verdict is not a gate."""
    _pin_sqlite_version(monkeypatch, (3, 40, 1))
    checks: list[preflight.Check] = []
    preflight.check_sqlite(checks, durable=False)
    assert _status(checks, "sqlite-wal-admission") == preflight.SKIP


def test_durable_profile_blocks_an_unverified_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_sqlite_version(monkeypatch, (3, 40, 1))
    checks: list[preflight.Check] = []
    preflight.check_sqlite(checks, durable=True)
    assert _status(checks, "sqlite-wal-admission") == preflight.BLOCKED


def test_durable_profile_blocks_a_declared_unverified_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declaration admits a store; it never makes the host a supported pilot."""
    _pin_sqlite_version(monkeypatch, (3, 40, 1))
    monkeypatch.setenv(UNVERIFIED_SQLITE_DECLARATION, "1")
    checks: list[preflight.Check] = []
    preflight.check_sqlite(checks, durable=True)
    assert _status(checks, "sqlite-wal-admission") == preflight.BLOCKED
    assert "never a supported deployment" in checks[-1].detail


def test_durable_profile_passes_a_fixed_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_sqlite_version(monkeypatch, (3, 53, 1))
    checks: list[preflight.Check] = []
    preflight.check_sqlite(checks, durable=True)
    assert _status(checks, "sqlite-wal-admission") == preflight.PASS


def test_a_refused_store_is_reported_without_leaving_a_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store gate check must agree with the store, not with a guess."""
    from cairn.infra.sessions.models import SessionStoreError

    def refuse(*args: object, **kwargs: object) -> None:
        raise SessionStoreError("sqlite_wal_unverified")

    monkeypatch.setattr(preflight, "SessionStore", refuse)
    monkeypatch.setattr(preflight, "wal_admission", lambda **kwargs: "refused")
    checks: list[preflight.Check] = []
    preflight.check_store_admission(checks, durable=True)
    assert _status(checks, "session-store-gate") == preflight.PASS
    assert "no directory created" in checks[-1].detail


def test_a_declared_unverified_store_still_blocks_durable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store that opens as `unverified` must still block the durable profile."""

    class UnverifiedStore:
        wal_admission = "unverified"

        def close(self) -> None:
            return None

    monkeypatch.setattr(preflight, "SessionStore", lambda *a, **k: UnverifiedStore())
    checks: list[preflight.Check] = []
    preflight.check_store_admission(checks, durable=True)
    assert _status(checks, "session-store-gate") == preflight.BLOCKED


def test_state_directory_inside_the_workspace_is_blocked(tmp_path: Path) -> None:
    """The store must stay outside every Agent-writable root."""
    checks: list[preflight.Check] = []
    preflight.check_state_outside_workspace(checks, tmp_path, tmp_path / "state")
    assert _status(checks, "state-directory-isolation") == preflight.BLOCKED

    checks = []
    preflight.check_state_outside_workspace(checks, tmp_path, tmp_path.parent / "s")
    assert _status(checks, "state-directory-isolation") == preflight.PASS


def test_preflight_contains_no_provider_or_network_code() -> None:
    """The documented command must stay offline and unpaid."""
    source = (ROOT / "scripts/pilot/preflight.py").read_text(encoding="utf-8")
    for forbidden in ("litellm", "requests", "urllib", "socket", "http.client"):
        assert forbidden not in source, f"preflight references {forbidden}"


def test_stateless_never_constructs_a_session_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stateless preflight must not open SQLite at all."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("stateless preflight constructed a SessionStore")

    monkeypatch.setattr(preflight, "SessionStore", explode)
    checks: list[preflight.Check] = []
    preflight.check_store_admission(checks, durable=False)

    assert _status(checks, "session-store-gate") == preflight.SKIP


def test_local_shell_check_reports_a_working_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pilot needs a shell that starts in the Workspace, not a sandbox."""
    from cairn.core.models import ToolResult

    class WorkingBashTool:
        def __init__(self, workspace: object) -> None:
            self.workspace = workspace

        async def execute(self, arguments: dict[str, object]) -> ToolResult:
            return ToolResult(stdout=str(self.workspace.root), stderr="", exit_code=0)

    monkeypatch.setattr(preflight, "BashTool", WorkingBashTool)
    checks: list[preflight.Check] = []
    preflight.check_local_shell(checks)

    assert _status(checks, "bash-shell") == preflight.PASS
    # The preflight states plainly that no isolation is claimed.
    assert _status(checks, "bash-isolation") == preflight.PASS


def test_local_shell_check_blocks_a_shell_that_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.core.models import ToolResult

    class BrokenBashTool:
        def __init__(self, _workspace: object) -> None:
            pass

        async def execute(self, arguments: dict[str, object]) -> ToolResult:
            return ToolResult(stdout="", stderr="no shell", exit_code=127)

    monkeypatch.setattr(preflight, "BashTool", BrokenBashTool)
    checks: list[preflight.Check] = []
    preflight.check_local_shell(checks)

    assert _status(checks, "bash-shell") == preflight.BLOCKED
