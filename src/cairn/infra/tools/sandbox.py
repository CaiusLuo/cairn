"""Sandbox writable-root resolution shared by the bash tool and its callers."""

from collections.abc import Mapping
from pathlib import Path


def _resolve_writable_root(
    raw_path: str | None,
    *,
    require_directory: bool,
) -> Path | None:
    """Resolve a candidate writable root, rejecting unsafe values.

    Returns ``None`` when the value is empty, relative, the filesystem root, or
    (when ``require_directory`` is set) not an existing directory. A malformed
    value can therefore only narrow the sandbox, never broaden it.
    """
    if not raw_path or not raw_path.strip():
        return None

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        return None

    try:
        resolved = candidate.resolve()
        if require_directory and not resolved.is_dir():
            return None
    except (OSError, RuntimeError):
        return None

    if resolved == Path(resolved.anchor):
        return None
    return resolved


def resolve_tmpdir(env: Mapping[str, str]) -> Path | None:
    """Resolve TMPDIR into a safe writable root for the macOS sandbox.

    TMPDIR must be an absolute path to an existing directory that is not the
    filesystem root.
    """
    return _resolve_writable_root(env.get("TMPDIR"), require_directory=True)


def resolve_uv_cache_dir(env: Mapping[str, str]) -> Path | None:
    """Resolve uv's effective cache root from the command environment.

    Mirrors uv's own lookup order: ``UV_CACHE_DIR``, then ``XDG_CACHE_HOME/uv``,
    then ``HOME/.cache/uv``. uv ignores an empty or relative ``XDG_CACHE_HOME``
    and falls back to ``HOME/.cache``; this resolver does the same rather than
    returning a relative writable root. An explicit but empty ``UV_CACHE_DIR``
    is malformed and fails closed. The returned directory does not have to exist
    yet: the sandbox can create the leaf when its parent already exists.
    """
    uv_cache_dir = env.get("UV_CACHE_DIR")
    if uv_cache_dir is not None:
        if not uv_cache_dir.strip():
            return None
        return _resolve_writable_root(uv_cache_dir, require_directory=False)

    xdg_cache_home = env.get("XDG_CACHE_HOME")
    if xdg_cache_home and Path(xdg_cache_home).is_absolute():
        return _resolve_writable_root(
            str(Path(xdg_cache_home) / "uv"), require_directory=False
        )

    home = env.get("HOME")
    if home and home.strip():
        return _resolve_writable_root(
            str(Path(home) / ".cache" / "uv"), require_directory=False
        )
    return None
