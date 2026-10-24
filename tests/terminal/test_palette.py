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
    assert "Enter" in _open("/").hint()


def test_closing_forgets_the_choice() -> None:
    palette = _open("/")
    palette.move(2)
    palette.close()

    assert not palette.open
    assert palette.selected_command() == ""


# -- levels ---------------------------------------------------------------
#
# The palette is a stack, not a flat list. `/model` descends into what that command
# accepts instead of running something that prints into the conversation, and Left
# comes back. The keys mean the same thing at every level.


def test_a_descending_command_opens_a_level() -> None:
    palette = _open("/model")
    assert palette.descend()
    assert palette.depth == 2


def test_the_child_level_lists_the_subcommands() -> None:
    palette = _open("/model")
    palette.descend()

    names = [row.name for row in palette.rows()]
    assert "use" in names
    assert "list" in names


def test_a_plain_command_does_not_descend() -> None:
    """`/compact` acts; it has nothing to choose from."""
    palette = _open("/compact")
    assert not palette.descend()
    assert palette.depth == 1


def test_left_returns_to_the_previous_level() -> None:
    palette = _open("/model")
    palette.descend()

    assert palette.ascend()
    assert palette.depth == 1
    assert palette.selected_name() == "/model"


def test_left_at_the_root_does_nothing() -> None:
    """The root has no parent; leaving it is what Esc is for."""
    palette = _open("/")
    assert not palette.ascend()
    assert palette.depth == 1


def test_the_breadcrumb_names_the_level() -> None:
    palette = _open("/model")
    palette.descend()

    assert palette.breadcrumb() == "Commands > /model"


def test_the_root_breadcrumb_is_just_commands() -> None:
    assert _open("/").breadcrumb() == "Commands"


def test_a_chosen_child_forms_the_command_line() -> None:
    """Choosing `use` under /model means `/model use`, which the router knows."""
    palette = _open("/model")
    palette.descend()
    palette.move(0)

    assert palette.command_line() == "/model use"


def test_the_command_line_at_the_root_is_the_command() -> None:
    assert _open("/model").command_line() == "/model"


def test_the_hint_changes_when_a_level_is_open() -> None:
    """Back is only meaningful below the root, so it is only offered there."""
    root = _open("/").hint()
    palette = _open("/model")
    palette.descend()

    assert "Left" not in root
    assert "Left" in palette.hint()


def test_typing_returns_to_the_root_level() -> None:
    """The filter applies to the command list, not to a submenu."""
    palette = _open("/model")
    palette.descend()

    palette.update("/mod")

    assert palette.depth == 1


def test_closing_forgets_the_navigation() -> None:
    palette = _open("/model")
    palette.descend()
    palette.close()

    assert not palette.open
    assert palette.breadcrumb() == ""
    assert palette.command_line() == ""


def test_a_level_keeps_its_own_selection() -> None:
    """Coming back must not lose the row the operator had chosen."""
    palette = _open("/")
    palette.move(1)
    chosen = palette.selected_name()
    palette.descend()
    palette.ascend()

    assert palette.selected_name() == chosen
