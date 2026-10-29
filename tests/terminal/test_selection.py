"""Composer selection tokens: plain text, colour by lexer, exact identity parse."""

from prompt_toolkit.document import Document

from cairn.terminal.selection import (
    SelectionTokenLexer,
    format_token,
    parse_selection_tokens,
)


def test_tokens_parse_to_task_and_identities() -> None:
    selection = parse_selection_tokens("[skill:local] fix [mcp:github] the bug")
    assert selection.task_text == "fix the bug"
    assert selection.skills == ("local",)
    assert selection.mcp_servers == ("github",)


def test_duplicate_tokens_are_deduplicated_in_order() -> None:
    selection = parse_selection_tokens("[skill:a] [skill:b] [skill:a] go")
    assert selection.skills == ("a", "b")
    assert selection.task_text == "go"


def test_format_token_is_plain_text() -> None:
    token = format_token("skill", "local")
    assert token == "[skill:local]"
    assert "\x1b[" not in token


def test_lexer_colours_token_without_ansi_in_text() -> None:
    lexer = SelectionTokenLexer()
    fragments = lexer.lex_document(Document("run [skill:local] now"))(0)
    assert ("class:selection-token", "[skill:local]") in fragments
    assert all("\x1b[" not in str(fragment[1]) for fragment in fragments)


def test_selection_preserves_pasted_code_whitespace() -> None:
    code = 'def f():\n    value = "two  spaces"\n\treturn value\n'
    assert parse_selection_tokens("[skill:local] " + code).task_text == code
    assert parse_selection_tokens("[skill:local]\n" + code).task_text == code
