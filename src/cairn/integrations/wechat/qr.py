"""Render a whole QR code as terminal half-block glyphs (no ANSI, no image)."""

from __future__ import annotations

import shutil

import qrcode  # type: ignore[import-untyped]


def render_ascii_qr(
    content: str, *, max_width: int | None = None, border: int = 2
) -> str:
    """Render the complete QR matrix in a bounded terminal area.

    One character represents one module horizontally and two modules vertically
    (half-block), so the rendered code keeps roughly square modules without
    wrapping. The quiet zone is included by the QR matrix; a narrow terminal
    raises instead of printing a clipped, unscannable code.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=border,
    )
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    width = max_width or shutil.get_terminal_size((80, 24)).columns
    modules = len(matrix[0]) if matrix else 0
    if modules == 0:
        raise ValueError("empty QR matrix")
    if modules > width:
        raise ValueError(
            f"QR requires {modules} columns but the terminal is {width} wide; "
            "open the fallback URL instead"
        )
    lines: list[str] = []
    for index in range(0, len(matrix), 2):
        top = matrix[index]
        bottom = matrix[index + 1] if index + 1 < len(matrix) else [False] * modules
        line = "".join(
            "█" if a and b else "▀" if a else "▄" if b else " "
            for a, b in zip(top, bottom, strict=True)
        )
        lines.append(line)
    return "\n".join(lines)
