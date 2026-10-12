"""Durable replacement of one local run artifact.

Reports, experiment records and derived Markdown are the durable evidence an
offline run leaves behind, and a reader may look at them while the run is still
writing. Every artifact is therefore replaced atomically: a reader sees either
the complete previous file or the complete next one, never a truncated one.
"""

import os
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile


def atomic_write_text(destination: Path, text: str) -> None:
    """Replace ``destination`` with ``text``, keeping the old file on any error."""
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
