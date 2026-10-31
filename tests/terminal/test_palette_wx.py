from __future__ import annotations

from cairn.terminal.palette import Palette


def test_wx_palette_lists_secondary_commands() -> None:
    palette = Palette()
    palette.update("/wx ")
    names = [row.name for row in palette.rows()]
    assert "disconnect" in names
    assert "stop" in names
    assert "help" in names
    palette.update("/wx dis")
    assert [row.name for row in palette.rows()] == ["disconnect"]
