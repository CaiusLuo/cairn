from prompt_toolkit import PromptSession
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.output import Output


def _create_key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def submit(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def insert_newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    return bindings


class CliInput:
    """Read complete user turns from an interactive terminal prompt.

    Prompt Toolkit's conventional Ctrl+D behavior is retained: it raises EOF on
    an empty buffer and otherwise edits the buffer without submitting it.
    """

    def __init__(
        self,
        *,
        input: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self._session: PromptSession[str] = PromptSession(
            multiline=True,
            key_bindings=_create_key_bindings(),
            input=input,
            output=output,
        )

    async def read(self) -> str:
        return await self._session.prompt_async("cairn> ")
