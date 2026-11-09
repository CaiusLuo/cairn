"""Open a private Session store outside scratch and Agent-writable roots."""

import os
from pathlib import Path

from cairn.agent.session.store import SessionStore
from cairn.agent.tools.bash import (
    build_command_env,
    resolve_tmpdir,
    resolve_uv_cache_dir,
)
from cairn.agent.workspace.workspace import Workspace


def open_store(
    directory: Path | None,
    workspace: Workspace,
    *,
    inspect: bool = False,
    secret_env_keys: frozenset[str] = frozenset(),
    sensitive_values: tuple[str, ...] = (),
    additional_roots: tuple[Path, ...] = (),
    allow_unverified: bool = False,
) -> SessionStore:
    # A store inside a scratch or cache directory can be deleted under a live
    # session, so those roots are refused regardless of the shell's sandboxing.
    # Match the child environment rather than raw host variables, because
    # credential filtering may have removed some of them.
    env = build_command_env(os.environ, secret_env_keys)
    roots = [workspace.root, *additional_roots]
    for path in (resolve_tmpdir(env), resolve_uv_cache_dir(env)):
        if path is not None:
            roots.append(path)
    return SessionStore(
        directory or Path.home() / ".local/state/cairn/sessions",
        mode="inspect" if inspect else "writer",
        forbidden_roots=tuple(roots),
        sensitive_values=sensitive_values,
        allow_unverified=allow_unverified,
    )
