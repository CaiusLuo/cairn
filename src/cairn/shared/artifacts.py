"""Durable, atomic replacement of one local artifact file.

Reports, experiment records and derived Markdown are the durable evidence a local
run leaves behind, and a reader may look at them while the run is still writing.
Every artifact is therefore replaced atomically: a reader sees either the complete
previous file or the complete next one, never a truncated one.

The write itself is not a trust decision. A caller that must keep its artifact
outside a working tree owns that check, because only the caller knows which tree
is sensitive.
"""

import os
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4


def atomic_write_text(destination: Path, text: str) -> None:
    """Replace ``destination`` with ``text``, keeping the old file on any error.

    The temporary file is created beside the destination so the final
    ``os.replace`` is a same-filesystem rename. A cleanup failure does not mask
    the original error, and an error raised while replacing still propagates
    rather than being swallowed.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            delete=False,
            prefix=f".{destination.name}-",
            suffix=".tmp",
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise


def atomic_write_private_bytes(path: Path, data: bytes) -> None:
    """Replace a file with 0600 content and sync its directory entry."""
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
