"""Helpers for tests that run commands through the Bash tool.

Cairn is local-first: the Bash tool executes on the host with no OS sandbox, so
there is nothing to probe for readiness. These helpers remain because the tests
that used them express a real precondition — "a local shell can run here" — and
they document that the old sandbox requirement is gone rather than silently
dropped.
"""

import sys
from pathlib import Path

import pytest

from cairn.infra.tools.bash import workspace_python
from cairn.infra.workspace.workspace import Workspace


def require_local_shell() -> None:
    """Skip when this host has no usable shell for the Bash tool.

    A local shell is the only requirement now; the platform sandbox is not.
    """
    if sys.platform == "win32":
        pytest.skip("Bash tool requires a POSIX shell")
    if not Path("/bin/bash").is_file():
        pytest.skip("/bin/bash is not available")


def require_execution_sandbox() -> None:
    """Skip when the bubblewrap execution sandbox cannot run on this host.

    Behavior checks that execute model-generated code refuse to fall back to the
    host, so a host without bwrap/user-namespaces cannot exercise them.
    """
    from cairn.infra.sandbox import available

    if not available():
        pytest.skip("bubblewrap execution sandbox is not available on this host")


def sandbox_python() -> str:
    """Return a Python executable usable by a command on this host."""
    interpreter = workspace_python()
    if not interpreter:
        pytest.skip("no Python interpreter available")
    return interpreter


def workspace_interpreter(workspace: Workspace) -> str:
    """Return the interpreter a command in ``workspace`` should use."""
    interpreter = workspace_python(workspace.root)
    if not interpreter:
        pytest.skip("no Python interpreter available")
    return interpreter
