"""The command palette: what `/` offers while it is being typed.

The palette is a temporary surface. It opens when the operator starts a command,
filters as they type, and closes without leaving anything in the transcript — a list
of commands is a means to an action, not a record of one.
"""

from cairn.terminal.palette import COMMANDS, MAX_ROWS, Palette


def _open(typed: str = "/") -> Palette:
    palette = Palette()
    palette.update(typed)
    return palette


def test_the_palette_is_closed_until_a_command_is_started() -> None:
    assert not Palette().open


def test_typing_a_slash_opens_it() -> None:
    assert _open("/").open


def test_ordinary_text_does_not_open_it() -> None:
    palette = Palette()
    palette.update("hello")
    assert not palette.open


def test_deleting_the_slash_closes_it() -> None:
    """The palette follows the text, so changing your mind closes it."""
    palette = _open("/mo")
    palette.update("mo")
    assert not palette.open


def test_an_empty_slash_offers_every_command() -> None:
    assert len(_open("/").matches()) == len(COMMANDS)


def test_typing_filters_by_prefix() -> None:
    names = [name for name, _ in _open("/mo").matches()]
    assert names == ["/model"]


def test_a_substring_that_is_not_a_prefix_does_not_match() -> None:
    """`/m` must not answer with `/compact` because it contains an m."""
    names = [name for name, _ in _open("/m").matches()]
    assert "/compact" not in names
    assert "/model" in names


def test_an_unknown_prefix_matches_nothing() -> None:
    assert _open("/zzz").matches() == ()


def test_matching_is_case_insensitive() -> None:
    assert [name for name, _ in _open("/MO").matches()] == ["/model"]


def test_the_selection_starts_at_the_first_row() -> None:
    rows = _open("/").rows()
    assert rows[0].selected
    assert not any(row.selected for row in rows[1:])


def test_moving_down_changes_the_selection() -> None:
    palette = _open("/")
    palette.move(1)
    assert [row.selected for row in palette.rows()].index(True) == 1


def test_the_selection_stops_at_either_end() -> None:
    palette = _open("/")
    palette.move(-1)
    assert palette.selected_command() == COMMANDS[0][0]

    for _ in range(len(COMMANDS) + 5):
        palette.move(1)
    assert palette.selected_command() == COMMANDS[-1][0]


def test_the_chosen_command_is_returned() -> None:
    palette = _open("/prov")
    assert palette.selected_command() == "/provider"


def test_nothing_is_chosen_when_nothing_matches() -> None:
    assert _open("/zzz").selected_command() == ""


def test_no_more_than_the_row_limit_is_shown() -> None:
    assert len(_open("/").rows()) <= MAX_ROWS


def test_the_hint_appears_only_while_open() -> None:
    assert Palette().hint() == ""
    assert "Tab" in _open("/").hint()


def test_closing_forgets_the_choice() -> None:
    palette = _open("/")
    palette.move(2)
    palette.close()

    assert not palette.open
    assert palette.selected_command() == ""
