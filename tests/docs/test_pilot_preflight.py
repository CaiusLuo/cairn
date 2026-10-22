"""Keep the pilot preflight's verdict honest.

The preflight is the operator's only offline admission gate, so its two rules
must hold: a refused SQLite runtime blocks the durable profile but never the
stateless one, and a declared-unverified runtime is never reported as supported.
These tests drive the real check functions; they do not reimplement them.
"""

from __future__ import annotations

import sqlite3
import sys
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


def test_a_sandbox_that_allows_an_outside_write_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An escape that succeeds must be reported BLOCKED, never PASS.

    The marker lives inside the TemporaryDirectory, so it has to be observed
    before that directory is deleted. A fake BashTool that really performs the
    outside write is the only way to prove the preflight notices it.
    """
    from cairn.core.models import ToolResult

    class EscapingBashTool:
        """Mimic a broken sandbox: the outside write actually lands."""

        def __init__(self, _workspace: object) -> None:
            pass

        async def execute(self, arguments: dict[str, object]) -> ToolResult:
            command = str(arguments["command"])
            if ">" not in command:
                return ToolResult(stdout="sandbox-ready", stderr="", exit_code=0)
            # The broken sandbox lets the outside write land.
            target = command.rsplit(">", 1)[1].strip()
            Path(target).write_text("leaked", encoding="utf-8")
            return ToolResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(preflight, "BashTool", EscapingBashTool)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    checks: list[preflight.Check] = []
    preflight.check_sandbox(checks)

    assert _status(checks, "bash-sandbox") == preflight.BLOCKED
    blocked = next(c for c in checks if c.name == "bash-sandbox")
    assert "landed outside the workspace" in blocked.detail


def test_a_refusing_sandbox_is_reported_as_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same check must still PASS when the outside write is refused."""
    from cairn.core.models import ToolResult

    class RefusingBashTool:
        def __init__(self, _workspace: object) -> None:
            pass

        async def execute(self, arguments: dict[str, object]) -> ToolResult:
            if ">" in str(arguments["command"]):
                return ToolResult(
                    stdout="",
                    stderr="permission denied",
                    exit_code=1,
                )
            return ToolResult(stdout="sandbox-ready", stderr="", exit_code=0)

    monkeypatch.setattr(preflight, "BashTool", RefusingBashTool)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    checks: list[preflight.Check] = []
    preflight.check_sandbox(checks)

    assert _status(checks, "bash-sandbox") == preflight.PASS


def test_a_macos_sandbox_that_allows_tmpdir_but_denies_outside_writes_is_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy macOS sandbox must not be mistaken for an escape.

    The macOS profile deliberately allows writes to the effective TMPDIR and the
    uv cache, and the preflight's own TemporaryDirectory lives under TMPDIR. A
    probe that dropped its escape marker there would let the (legitimate) TMPDIR
    allowance create it and report the sandbox BLOCKED. The marker must be probed
    outside every writable root, so this fake models the real profile: writes
    under the host temp directory succeed, writes anywhere else are refused.
    """
    import tempfile

    from cairn.core.models import ToolResult

    temp_root = Path(tempfile.gettempdir()).resolve()

    class MacOSSemanticsBashTool:
        def __init__(self, _workspace: object) -> None:
            pass

        async def execute(self, arguments: dict[str, object]) -> ToolResult:
            command = str(arguments["command"])
            if ">" not in command:
                return ToolResult(stdout="sandbox-ready", stderr="", exit_code=0)
            target = Path(command.rsplit(">", 1)[1].strip())
            try:
                target.resolve().relative_to(temp_root)
            except ValueError:
                # Outside the writable roots: the real profile denies it.
                return ToolResult(
                    stdout="", stderr="operation not permitted", exit_code=1
                )
            # Inside TMPDIR (a legitimate writable root): the write lands.
            target.write_text("leaked", encoding="utf-8")
            return ToolResult(stdout="", stderr="", exit_code=0)

    monkeypatch.setattr(preflight, "BashTool", MacOSSemanticsBashTool)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    checks: list[preflight.Check] = []
    preflight.check_sandbox(checks)

    assert _status(checks, "bash-sandbox") == preflight.PASS
    assert _status(checks, "sandbox-read-boundary") == preflight.PASS


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


def test_a_private_mount_write_with_exit_zero_is_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that only lands in the sandbox's private view is not an escape.

    On Linux the mount namespace gives the command a private /tmp tmpfs and
    creates workspace parents with --dir, so an outside write can succeed inside
    that view and exit 0 while the host is untouched. The host marker stays
    absent, which is the property under test, so the verdict is PASS.
    """
    from cairn.core.models import ToolResult

    class PrivateViewBashTool:
        """Write succeeds in the child's private layer; the host never sees it."""

        def __init__(self, _workspace: object) -> None:
            pass

        async def execute(self, arguments: dict[str, object]) -> ToolResult:
            if ">" in str(arguments["command"]):
                # Exit 0 exactly as the real Bubblewrap call does.
                return ToolResult(stdout="", stderr="", exit_code=0)
            return ToolResult(stdout="sandbox-ready", stderr="", exit_code=0)

    monkeypatch.setattr(preflight, "BashTool", PrivateViewBashTool)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    checks: list[preflight.Check] = []
    preflight.check_sandbox(checks)

    assert _status(checks, "bash-sandbox") == preflight.PASS


@pytest.mark.parametrize("escape_exit", [0, 1, 2])
def test_a_real_host_write_is_blocked_whatever_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, escape_exit: int
) -> None:
    """A genuine host write must BLOCK regardless of what the command reports."""
    from cairn.core.models import ToolResult

    class LeakingBashTool:
        def __init__(self, _workspace: object) -> None:
            pass

        async def execute(self, arguments: dict[str, object]) -> ToolResult:
            command = str(arguments["command"])
            if ">" not in command:
                return ToolResult(stdout="sandbox-ready", stderr="", exit_code=0)
            target = command.rsplit(">", 1)[1].strip()
            Path(target).write_text("leaked", encoding="utf-8")
            return ToolResult(stdout="", stderr="", exit_code=escape_exit)

    monkeypatch.setattr(preflight, "BashTool", LeakingBashTool)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    checks: list[preflight.Check] = []
    preflight.check_sandbox(checks)

    assert _status(checks, "bash-sandbox") == preflight.BLOCKED
    assert _status(checks, "sandbox-read-boundary") == preflight.BLOCKED


def test_a_sandbox_that_cannot_run_commands_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the sandbox cannot even execute a command, the verdict is BLOCKED."""
    from cairn.core.models import ToolResult

    class BrokenBashTool:
        def __init__(self, _workspace: object) -> None:
            pass

        async def execute(self, arguments: dict[str, object]) -> ToolResult:
            return ToolResult(stdout="", stderr="bwrap: failed to set up", exit_code=1)

    monkeypatch.setattr(preflight, "BashTool", BrokenBashTool)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    checks: list[preflight.Check] = []
    preflight.check_sandbox(checks)

    assert _status(checks, "bash-sandbox") == preflight.BLOCKED
    blocked = next(c for c in checks if c.name == "bash-sandbox")
    assert "bwrap" in blocked.detail


def test_a_missing_sandbox_binary_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sandbox executable means no sandboxed execution at all."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "is_file", lambda self: False)

    checks: list[preflight.Check] = []
    preflight.check_sandbox(checks)

    assert _status(checks, "bash-sandbox") == preflight.BLOCKED
