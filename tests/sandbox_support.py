"""Helpers for tests that run commands through the Bash tool's OS sandbox."""

import asyncio
import sys
from pathlib import Path

import pytest

from cairn.tools.bash import BashTool
from cairn.workspace.workspace import Workspace


def skip_without_sandbox() -> None:
    """Skip when the host has no sandbox implementation for this platform."""
    if sys.platform == "darwin":
        if not Path("/usr/bin/sandbox-exec").is_file():
            pytest.skip("sandbox-exec is not available")
    elif sys.platform == "linux":
        if not Path("/usr/bin/bwrap").is_file():
            pytest.skip("bubblewrap is not installed")
    else:
        pytest.skip(f"BashTool has no sandbox for {sys.platform}")


def require_working_sandbox(workspace: Workspace) -> None:
    """Skip when the sandbox exists but cannot actually enforce.

    A host may lack user namespaces, may be missing the seatbelt entitlement, or
    may already run inside another sandbox that refuses nested sandbox
    application. Those are environment limits, not test failures.
    """
    skip_without_sandbox()

    result = asyncio.run(
        BashTool(workspace).execute({"command": "printf sandbox-ready"})
    )
    if result.exit_code != 0 or result.stdout != "sandbox-ready":
        pytest.skip(f"sandbox unavailable: {result.stderr.strip()}")


def sandbox_python() -> str:
    """Return a Python executable that exists inside the sandbox."""
    candidates = (
        [sys.executable]
        if sys.platform == "darwin"
        else ["/usr/bin/python3", sys.executable]
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    pytest.skip("no Python interpreter available inside the sandbox")
