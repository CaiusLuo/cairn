import asyncio

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cairn.input import CliInput

BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"


async def _assert_still_editing(read_task: asyncio.Task[str]) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(read_task), timeout=0.05)


def test_bracketed_multiline_paste_waits_for_explicit_submit() -> None:
    async def scenario() -> None:
        pasted = "line 1\nline 2\nline 3"
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            read_task = asyncio.create_task(cli_input.read())

            pipe_input.send_text(
                f"{BRACKETED_PASTE_START}{pasted}{BRACKETED_PASTE_END}"
            )
            await _assert_still_editing(read_task)

            pipe_input.send_text("\r")
            assert await asyncio.wait_for(read_task, timeout=1) == pasted

    asyncio.run(scenario())


def test_enter_submits_single_line() -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            pipe_input.send_text("hello\r")

            assert await asyncio.wait_for(cli_input.read(), timeout=1) == "hello"

    asyncio.run(scenario())


def test_meta_enter_inserts_newline_instead_of_submitting() -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            read_task = asyncio.create_task(cli_input.read())

            pipe_input.send_text("line 1\x1b\r")
            await _assert_still_editing(read_task)

            pipe_input.send_text("line 2\r")
            assert await asyncio.wait_for(read_task, timeout=1) == "line 1\nline 2"

    asyncio.run(scenario())


def test_bracketed_paste_preserves_markdown_code_fence() -> None:
    async def scenario() -> None:
        pasted = '```python\nif ready:\n    print("hello")\n```'
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            read_task = asyncio.create_task(cli_input.read())

            pipe_input.send_text(
                f"{BRACKETED_PASTE_START}{pasted}{BRACKETED_PASTE_END}"
            )
            await _assert_still_editing(read_task)

            pipe_input.send_text("\r")
            assert await asyncio.wait_for(read_task, timeout=1) == pasted

    asyncio.run(scenario())


def test_ctrl_c_cancels_current_input_and_clears_buffer() -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            pipe_input.send_text("partial\x03")

            with pytest.raises(KeyboardInterrupt):
                await cli_input.read()

            pipe_input.send_text("fresh\r")
            assert await asyncio.wait_for(cli_input.read(), timeout=1) == "fresh"

    asyncio.run(scenario())


def test_ctrl_d_on_empty_buffer_raises_eof() -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            pipe_input.send_text("\x04")

            with pytest.raises(EOFError):
                await cli_input.read()

    asyncio.run(scenario())


def test_ctrl_d_on_non_empty_buffer_does_not_submit() -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            read_task = asyncio.create_task(cli_input.read())

            pipe_input.send_text("partial\x04")
            await _assert_still_editing(read_task)

            pipe_input.send_text("\r")
            assert await asyncio.wait_for(read_task, timeout=1) == "partial"

    asyncio.run(scenario())
