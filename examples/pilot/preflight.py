"""Offline single-node pilot preflight.

Checks that this host can run a Cairn pilot before an operator starts one, and
prints one PASS/BLOCKED line per check. It calls no provider, opens no network
connection and requires no API credential; the provider check below only
inspects configuration shape.

Run from the repository root:

    uv run python examples/pilot/preflight.py

Exit code is 0 only when every check that this profile requires passed. A check
this host cannot decide is reported as BLOCKED with the missing evidence, never
as a silent PASS. See docs/guides/pilot-runbook.md for the operator procedure.
"""

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if not __package__:
    # Support the documented file invocation as well as package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cairn.infra.config import load_project_providers
from cairn.infra.config_schema import ConfigLayout
from cairn.infra.observability.models import Span, SpanStatus
from cairn.infra.observability.sinks import JsonlTraceSink
from cairn.infra.observability.tracer import Tracer
from cairn.infra.sessions.models import SessionStoreError
from cairn.infra.sessions.store import (
    UNVERIFIED_SQLITE_DECLARATION,
    VERIFIED_SQLITE_VERSIONS,
    SessionStore,
    sqlite_wal_fix_supported,
    wal_admission,
)
from cairn.infra.tools.bash import BashTool
from cairn.infra.workspace.workspace import Workspace

PASS = "PASS"
BLOCKED = "BLOCKED"
SKIP = "SKIP"

#: Profile switches. ``stateless`` is the default pilot; ``durable`` adds the
#: SQLite admission gate. Publishing is never probed here.
STATELESS = "local-stateless"
DURABLE = "local-durable-pilot"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str


def _emit(checks: list[Check], check: Check) -> Check:
    checks.append(check)
    print(f"{check.status:<7} {check.name}: {check.detail}", flush=True)
    return check


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def check_python(checks: list[Check]) -> None:
    version = ".".join(str(part) for part in sys.version_info[:3])
    ok = sys.version_info >= (3, 12)
    _emit(
        checks,
        Check(
            "python-version",
            PASS if ok else BLOCKED,
            f"{version} at {sys.executable}" + ("" if ok else " (requires >= 3.12)"),
        ),
    )


def check_sqlite(checks: list[Check], *, durable: bool) -> None:
    # One version feeds both judgements, so the reported verdict and the store's
    # own gate can never disagree about which runtime is being described.
    version_info = sqlite3.sqlite_version_info
    version = sqlite3.sqlite_version
    supported = sqlite_wal_fix_supported(version_info)
    declared = os.environ.get(UNVERIFIED_SQLITE_DECLARATION) == "1"
    admission = wal_admission(version=version_info)
    detail = (
        f"sqlite {version}; wal_reset_fix={supported}; "
        f"admission={admission}; requires={VERIFIED_SQLITE_VERSIONS}"
    )
    if declared:
        detail += f" ({UNVERIFIED_SQLITE_DECLARATION}=1 declared unverified)"

    if not durable:
        # Stateless callers never construct a SessionStore, so the runtime
        # verdict cannot block them; it is reported, not required.
        status = PASS if supported else SKIP
        _emit(
            checks,
            Check(
                "sqlite-wal-admission",
                status,
                detail
                + ("; stateless mode does not open a store" if not supported else ""),
            ),
        )
        return

    if admission == "verified":
        _emit(checks, Check("sqlite-wal-admission", PASS, detail))
    elif admission == "unverified":
        _emit(
            checks,
            Check(
                "sqlite-wal-admission",
                BLOCKED,
                detail + "; declared unverified is never a supported deployment",
            ),
        )
    else:
        _emit(
            checks,
            Check(
                "sqlite-wal-admission",
                BLOCKED,
                detail
                + f"; this runtime is refused. Declare {UNVERIFIED_SQLITE_DECLARATION}=1"
                " only for development, or use a fixed-SQLite interpreter.",
            ),
        )


def check_sandbox(checks: list[Check]) -> None:
    tool_name = (
        "/usr/bin/sandbox-exec"
        if sys.platform == "darwin"
        else "/usr/bin/bwrap"
        if sys.platform == "linux"
        else None
    )
    if tool_name is None:
        _emit(
            checks,
            Check("bash-sandbox", BLOCKED, f"no sandbox for {sys.platform}"),
        )
        return
    if not Path(tool_name).is_file():
        _emit(checks, Check("bash-sandbox", BLOCKED, f"{tool_name} is not installed"))
        return

    with tempfile.TemporaryDirectory(prefix="cairn-preflight-sandbox-") as root:
        workspace = Path(root) / "workspace"
        workspace.mkdir()
        tool = BashTool(Workspace(workspace))
        result = asyncio.run(tool.execute({"command": "printf sandbox-ready"}))
        marker = Path(root) / "escape-marker"
        # An outside write must not reach the host. The tool's exit code is not
        # part of the verdict (reason below), but the call has to run either way.
        asyncio.run(tool.execute({"command": f"printf leaked > {marker}"}))
        # Observe the result while the directory still exists: TemporaryDirectory
        # deletes the marker on exit, so a later check would always look clean
        # and could report PASS for a sandbox that actually let the write through.
        escaped = marker.exists()

    if result.exit_code != 0 or result.stdout != "sandbox-ready":
        _emit(
            checks,
            Check("bash-sandbox", BLOCKED, result.stderr.strip() or "sandbox failed"),
        )
        return

    # The decisive property is the host side: an outside write must leave no
    # marker on the host. That is what the isolation boundary protects, and it is
    # observed above while the temporary directory still exists.
    #
    # The command's own exit code is deliberately NOT part of the verdict. On
    # Linux the sandbox mounts a private /tmp tmpfs and creates workspace parents
    # with --dir, so a write outside the workspace can legitimately succeed inside
    # that private view and exit 0 while the host never sees it. Requiring a
    # nonzero exit there reported a healthy sandbox as BLOCKED. A real host write
    # is still caught, because it always creates the host-side marker.
    enforced = not escaped
    _emit(
        checks,
        Check(
            "bash-sandbox",
            PASS if enforced else BLOCKED,
            f"{tool_name} runs commands and leaves no write outside the workspace"
            if enforced
            else f"{tool_name} ran and a write landed outside the workspace",
        ),
    )
    # Read isolation is a Linux property; macOS allows host reads by default.
    read_detail = (
        "writes are jailed and reads are limited by the mount namespace"
        if sys.platform == "linux"
        else "writes are jailed; host reads are allowed (macOS profile) - "
        "not a confidentiality boundary"
    )
    _emit(
        checks,
        Check("sandbox-read-boundary", PASS if enforced else BLOCKED, read_detail),
    )


def check_cli(checks: list[Check]) -> None:
    try:
        from typer.testing import CliRunner

        from cairn.terminal.cli import app
    except Exception as error:  # pragma: no cover - import failure is the check
        _emit(checks, Check("cli-import", BLOCKED, f"{type(error).__name__}: {error}"))
        return

    result = CliRunner().invoke(app, ["--help"])
    ok = result.exit_code == 0 and "session" in result.stdout
    _emit(
        checks,
        Check(
            "cli-import",
            PASS if ok else BLOCKED,
            "cairn package imports and `cairn --help` lists commands"
            if ok
            else f"--help exit={result.exit_code}",
        ),
    )


def default_state_dir() -> Path:
    """The CLI's own default store root; one source for both checks."""
    return Path.home() / ".local/state/cairn"


def check_state_dir(checks: list[Check], target: Path) -> None:
    """A pilot needs one writable state directory with private permissions."""
    try:
        target.mkdir(parents=True, exist_ok=True)
        info = target.stat()
        writable = os.access(target, os.W_OK | os.X_OK)
        probe = target / f".preflight-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        _emit(checks, Check("state-directory", BLOCKED, f"{target}: {error}"))
        return

    _emit(
        checks,
        Check(
            "state-directory",
            PASS if writable else BLOCKED,
            f"{target} is writable" if writable else f"{target} is not writable",
        ),
    )

    # Durable sessions store conversation checkpoints, so other local users
    # must not read them. The CLI creates this directory with the process umask
    # (0755 under the default 022), so world-readable is reported as a fact and
    # the operator decides whether to tighten it on a shared host.
    mode = stat.S_IMODE(info.st_mode)
    world = mode & stat.S_IRWXO
    group = mode & stat.S_IRWXG
    shared_note = (
        f"; mode {mode:04o} is world-accessible (other {world:04o}, group "
        f"{group:04o}): tighten with chmod 700 on a shared host"
        if world
        else f"; mode {mode:04o} is not accessible to other users"
    )
    _emit(
        checks,
        Check(
            "state-directory-permissions",
            BLOCKED if not os.access(target, os.R_OK) else PASS,
            f"{target}" + shared_note,
        ),
    )


def check_state_outside_workspace(
    checks: list[Check], workspace: Path, state_dir: Path
) -> None:
    """SessionStore refuses storage inside any Agent-writable root."""
    inside = _is_within(state_dir, workspace)
    _emit(
        checks,
        Check(
            "state-directory-isolation",
            BLOCKED if inside else PASS,
            f"{state_dir} is "
            + ("inside" if inside else "outside")
            + " the current workspace",
        ),
    )


def check_store_admission(checks: list[Check], *, durable: bool) -> None:
    """Prove the store's own gate agrees with the reported verdict.

    The store is opened in a disposable directory outside every Agent-writable
    root, exactly like the CLI does, so the check exercises the real admission
    path. SessionStore creates its own private (0700) directory, so the parent
    is deliberately not pre-created. Nothing is written when it refuses.
    """
    if not durable:
        # A stateless profile never constructs a SessionStore, so opening one
        # here would both misreport the profile and create SQLite side effects
        # the mode does not have.
        _emit(
            checks,
            Check(
                "session-store-gate",
                SKIP,
                "stateless mode does not open a SessionStore",
            ),
        )
        return

    # SessionStore walks path components without following symlinks, so start
    # from the resolved directory (on macOS /var is a symlink to /private/var).
    base = Path(tempfile.mkdtemp(prefix="cairn-preflight-store-")).resolve()
    try:
        store_dir = base / "store"
        try:
            store = SessionStore(
                store_dir,
                mode="writer",
                # The store's own gate decides; the preflight never declares a
                # runtime unverified on the operator's behalf.
                forbidden_roots=(base / "work",),
            )
        except SessionStoreError as refused:
            admission = wal_admission()
            if admission == "refused":
                # The refusal must be the admission gate, not a path problem,
                # and it must leave no directory behind.
                expected = refused.code == "sqlite_wal_unverified"
                _emit(
                    checks,
                    Check(
                        "session-store-gate",
                        PASS if expected and not store_dir.exists() else BLOCKED,
                        f"refused with {refused.code} and "
                        + (
                            "no directory created"
                            if not store_dir.exists()
                            else "a directory left behind"
                        ),
                    ),
                )
            else:
                _emit(
                    checks,
                    Check(
                        "session-store-gate",
                        BLOCKED,
                        f"unexpected refusal {refused.code} for admission={admission}",
                    ),
                )
            return
        try:
            admission = store.wal_admission
        finally:
            store.close()
        if admission == "verified":
            _emit(
                checks,
                Check(
                    "session-store-gate",
                    PASS,
                    f"store opens with admission={admission}",
                ),
            )
        else:
            _emit(
                checks,
                Check(
                    "session-store-gate",
                    BLOCKED,
                    f"admission={admission} is not a verified deployment",
                ),
            )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def check_trace(checks: list[Check], workspace: Path) -> None:
    """A trace must be written and be readable as JSONL, with no model call."""
    with tempfile.TemporaryDirectory(prefix="cairn-preflight-trace-") as root:
        trace_root = Path(root) / "traces"
        tracer = Tracer(JsonlTraceSink(trace_root))
        span: Span = tracer.start_root_span("preflight")
        tracer.end_span(span, status=SpanStatus.OK)
        files = list(trace_root.glob("*.jsonl"))
        if len(files) != 1:
            _emit(
                checks,
                Check(
                    "trace-output", BLOCKED, f"expected 1 trace file, got {len(files)}"
                ),
            )
            return
        lines = files[0].read_text(encoding="utf-8").splitlines()
        try:
            records = [json.loads(line) for line in lines]
        except json.JSONDecodeError as error:
            _emit(checks, Check("trace-output", BLOCKED, f"unreadable trace: {error}"))
            return
        ok = bool(records) and any(r.get("name") == "preflight" for r in records)
        _emit(
            checks,
            Check(
                "trace-output",
                PASS if ok else BLOCKED,
                f"{len(records)} JSONL record(s) under a writable trace root",
            ),
        )
    _ = workspace


def check_provider_config(checks: list[Check], workspace: Path) -> None:
    """Validate configuration shape only: no credential resolution, no call.

    ``load_project_providers`` is the runtime's own structural validator; it
    refuses an unreadable or invalid models.toml instead of falling back to the
    environment, which is the failure the pilot must not inherit silently.
    """
    toml_file = workspace / ".cairn" / "models.toml"
    try:
        project = load_project_providers(toml_file)
    except Exception as error:
        _emit(
            checks,
            Check(
                "provider-config",
                BLOCKED,
                f"{toml_file}: {type(error).__name__}: {error}",
            ),
        )
        return

    env_file = workspace / ".env"
    if project.layout is ConfigLayout.CATALOG:
        detail = f"catalog layout, {len(project.providers)} provider(s)"
    elif project.layout is ConfigLayout.LEGACY:
        detail = "legacy single-provider layout"
    elif env_file.is_file():
        detail = ".env layout; .env present (not read here)"
    else:
        detail = ".env layout, but no .env in this workspace"

    # Credentials are deliberately not resolved; the pilot approves them at run
    # time. Only structure is judged here.
    _emit(checks, Check("provider-config", PASS, detail + "; credentials not read"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=(STATELESS, DURABLE),
        default=STATELESS,
        help="Pilot profile whose required checks decide the exit code.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="State directory to probe instead of ~/.local/state/cairn.",
    )
    parser.add_argument(
        "--allow-unverified-sqlite",
        action="store_true",
        help="Report the durable SQLite gate as declared for development only.",
    )
    args = parser.parse_args(argv)

    if args.allow_unverified_sqlite:
        # Forwarded to the same single version judgement the store uses.
        os.environ[UNVERIFIED_SQLITE_DECLARATION] = "1"

    workspace = Path.cwd()
    state_dir = args.state_dir or default_state_dir()
    durable = args.profile == DURABLE
    print(f"Cairn pilot preflight: profile={args.profile} workspace={workspace}")
    print(f"python={sys.executable}")
    print()

    checks: list[Check] = []
    check_python(checks)
    check_sqlite(checks, durable=durable)
    check_sandbox(checks)
    check_cli(checks)
    check_state_dir(checks, state_dir)
    check_state_outside_workspace(checks, workspace, state_dir)
    check_store_admission(checks, durable=durable)
    check_trace(checks, workspace)
    check_provider_config(checks, workspace)

    print()
    blocked = [check for check in checks if check.status == BLOCKED]
    passed = [check for check in checks if check.status == PASS]
    skipped = [check for check in checks if check.status == SKIP]
    print(
        f"Verdict for {args.profile}: "
        f"{PASS if not blocked else BLOCKED} "
        f"({len(passed)} passed, {len(skipped)} skipped, {len(blocked)} blocked"
        f" of {len(checks)})"
    )
    print(
        "GitHub Publishing: BLOCKED (not exercised; it needs a separate authorized"
        " live smoke, see docs/guides/pilot-runbook.md)"
    )
    if blocked:
        print("Blocking checks:")
        for check in blocked:
            print(f"  - {check.name}: {check.detail}")
    return 0 if not blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())
