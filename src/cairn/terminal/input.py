from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.output import Output
from prompt_toolkit.shortcuts.choice_input import ChoiceInput

from cairn.llm.model_manager import ModelConfig


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

    async def choose_model(
        self, provider: str, models: tuple[ModelConfig, ...], current: str
    ) -> str | None:
        """Inline group selection using the normal prompt's input and output."""
        bindings = KeyBindings()

        @bindings.add("escape")
        def cancel(event: KeyPressEvent) -> None:
            # Not eager: Escape also prefixes the terminal's arrow sequences.
            event.app.exit(exception=KeyboardInterrupt())

        picker = ChoiceInput(
            message=(
                f"Provider: {provider}\nSelect a model group\n"
                "Up/Down move · Enter select · Esc/Ctrl+C cancel"
            ),
            options=[
                (
                    model.name,
                    f"{model.name}{' (current)' if model.name == current else ''}\n"
                    f"  Primary: {model.model_ids[0]}\n"
                    f"  Fallbacks: {' -> '.join(model.model_ids[1:]) or 'none'}",
                )
                for model in models
            ],
            default=current,
            show_numbers=False,
            key_bindings=bindings,
        )
        # Reuse even explicitly injected pipe/PTY input. No second stdin reader
        # or blocking prompt is introduced inside the CLI's asyncio loop.
        with create_app_session(
            input=self._session.app.input, output=self._session.app.output
        ):
            try:
                return await picker.prompt_async()
            except (KeyboardInterrupt, EOFError):
                return None
