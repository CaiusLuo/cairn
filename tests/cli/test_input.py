import asyncio

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from cairn.input import CliInput


async def _assert_still_editing(read_task: asyncio.Task[str]) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(read_task), timeout=0.05)


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


def test_ctrl_c_clears_input_and_allows_a_fresh_turn() -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            pipe_input.send_text("partial\x03")

            with pytest.raises(KeyboardInterrupt):
                await cli_input.read()

            pipe_input.send_text("fresh\r")
            assert await asyncio.wait_for(cli_input.read(), timeout=1) == "fresh"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("input_text", "expected_text"),
    [("\x04", None), ("partial\x04", "partial")],
)
def test_ctrl_d_eof_and_partial_buffer_behavior(
    input_text: str, expected_text: str | None
) -> None:
    async def scenario() -> None:
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            if expected_text is not None:
                read_task = asyncio.create_task(cli_input.read())
                pipe_input.send_text(input_text)
                await _assert_still_editing(read_task)
                pipe_input.send_text("\r")
                assert await asyncio.wait_for(read_task, timeout=1) == expected_text
            else:
                pipe_input.send_text(input_text)
                with pytest.raises(EOFError):
                    await cli_input.read()

    asyncio.run(scenario())
