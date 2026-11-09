"""One bounded pipe-capture loop shared by every infra subprocess owner."""

import asyncio

PIPE_READ_CHUNK_SIZE = 16 * 1024


async def read_bounded(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read a pipe to EOF, retaining at most ``limit`` bytes.

    Returns the captured bytes and whether the producer emitted more. The loop
    always drains the stream so a full pipe cannot stall the process waiter.
    """
    captured = bytearray()
    truncated = False

    while True:
        chunk = await stream.read(PIPE_READ_CHUNK_SIZE)
        if not chunk:
            break

        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])

        if len(chunk) > remaining:
            truncated = True

    return bytes(captured), truncated
