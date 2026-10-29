"""Dynamic palette levels (skills/MCP), search, scrolling and token insertion."""

from cairn.terminal.palette import MAX_ROWS, Palette

ROOT = (("/skill", "Select a skill"), ("/mcp", "Select an MCP server"))
CHILDREN = {
    "/skill": tuple((f"s{i}", f"desc {i}") for i in range(9)),
    "/mcp": (("github", "streamable_http"),),
}


def _palette() -> Palette:
    palette = Palette()
    palette.set_dynamic(ROOT, CHILDREN)
    palette.update("/")
    return palette


def test_dynamic_root_merges_with_builtin_commands() -> None:
    names = [name for name, _ in _palette().matches()]
    assert "/skill" in names and "/mcp" in names and "/model" in names


def test_skill_level_lists_and_filters() -> None:
    palette = _palette()
    palette.update("/skill")
    assert next(row.name for row in palette.rows()) == "s0"
    palette.update("/skill desc 7")
    assert [row.name for row in palette.rows()] == ["s7"]


def test_selection_beyond_six_rows_is_reachable() -> None:
    palette = _palette()
    palette.update("/skill")
    for _ in range(20):
        palette.move(1)
    assert palette.selected_name() == "s8"
    rows = palette.rows()
    assert len(rows) <= MAX_ROWS
    assert rows[-1].name == "s8" and rows[-1].selected


def test_enter_token_for_skill_and_mcp() -> None:
    skill = _palette()
    skill.update("/skill")
    assert skill.insertion() == "[skill:s0]"
    mcp = _palette()
    mcp.update("/mcp")
    assert mcp.insertion() == "[mcp:github]"


def test_plain_command_has_no_insertion_token() -> None:
    palette = _palette()
    palette.update("/model")
    assert palette.insertion() == ""


def test_inline_second_selection_keeps_search_query() -> None:
    palette = _palette()
    text = "[skill:s0] do something /mcp git"
    palette.update(text)
    assert [r.name for r in palette.rows()] == ["github"]
    assert palette.command_offset() == text.index("/mcp")
    palette.update("[mcp:github] /skill desc 7")
    assert palette.insertion() == "[skill:s7]"
