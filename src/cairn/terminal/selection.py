"""Composer selection tokens for skills and MCP servers.

A token is short, ASCII-only text such as ``[skill:local]``; colour is applied by
a lexer, never by embedding escape codes in the buffer. The visible buffer thus
stays readable in a no-colour terminal, and parsing the buffer recovers the exact
identity the runtime must resolve.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.lexers import Lexer

TOKEN_RE = re.compile(r"\[(skill|mcp):([\w-]+)\]")


def format_token(kind: str, name: str) -> str:
    return f"[{kind}:{name}]"


@dataclass(frozen=True, slots=True)
class Selection:
    task_text: str
    skills: tuple[str, ...]
    mcp_servers: tuple[str, ...]


def parse_selection_tokens(text: str) -> Selection:
    """Split composer text into the task and the selected identities."""
    matches = list(TOKEN_RE.finditer(text))
    if not matches:
        # No selection: return the operator's text unchanged so multiline pastes
        # and spacing keep their existing behavior.
        return Selection(task_text=text, skills=(), mcp_servers=())
    skills: list[str] = []
    servers: list[str] = []
    for match in matches:
        kind, name = match.group(1), match.group(2).strip()
        if not name:
            continue
        (skills if kind == "skill" else servers).append(name)
    # Remove only token spans and their single insertion separator. Never
    # normalize the surrounding task: it may be indentation-sensitive code.
    parts: list[str] = []
    position = 0
    for match in matches:
        parts.append(text[position : match.start()])
        position = match.end()
        if text[position : position + 1] == " " or (
            text[position : position + 1] == "\n"
            and (match.start() == 0 or text[match.start() - 1] == "\n")
        ):
            position += 1
    parts.append(text[position:])
    task = "".join(parts)
    return Selection(
        task_text=task,
        skills=tuple(dict.fromkeys(skills)),
        mcp_servers=tuple(dict.fromkeys(servers)),
    )


class SelectionTokenLexer(Lexer):
    """Colour selection tokens without changing the underlying text."""

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        lines = document.lines

        def get_line(lineno: int) -> StyleAndTextTuples:
            line = lines[lineno]
            fragments: StyleAndTextTuples = []
            position = 0
            for match in TOKEN_RE.finditer(line):
                if match.start() > position:
                    fragments.append(("class:input", line[position : match.start()]))
                fragments.append(("class:selection-token", match.group(0)))
                position = match.end()
            if position < len(line):
                fragments.append(("class:input", line[position:]))
            if not fragments:
                fragments.append(("class:input", line))
            return fragments

        return get_line
