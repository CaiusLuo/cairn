"""A pinned input line at the bottom, with the terminal's own scrollback above.

The design follows one decision: **do not own the screen**. A full-screen
application would let the input be pinned absolutely, but it takes the terminal's
scrollback with it — no native search, no tmux copy mode, no drag-select — and that
is a bad trade for a tool whose output is read back for hours. Research into the
shipped alternatives agrees: the products that kept scrollback (Codex, Gemini's
default layout) reserve only a bottom viewport, and the ones that took the whole
screen document the loss as a known cost.

So finalized output is printed normally, into real scrollback, where every
terminal feature already works. This application owns only what must repaint: the
live tail and the input line. The input therefore sits at the bottom in practice,
pushed there by the output above it.

Rendering rules this module owns:

- **Foreground only.** No widget sets a background colour. A filled pane looks
  fine locally and becomes unreadable through a remote terminal with a different
  palette, so hierarchy is brightness and weight instead of fill.
- **Paced repaint.** Streamed text is admitted by a frame budget, so a fast model
  does not produce a flickering pane.
- **Ctrl-C is bound.** A prompt_toolkit application swallows SIGINT by default, so
  an unbound Ctrl-C leaves the user unable to interrupt anything.
"""

import time
from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from cairn.terminal.pacing import StreamPacer
from cairn.terminal.transcript import TRANSCRIPT_GAP_LINES, Transcript

#: Prompt shown at the input line. Kept short so the input keeps its width.
INPUT_PROMPT = "cairn> "

#: Styling for the owned region. Colours are foreground-only on purpose: a filled
#: pane is indistinguishable from unreadable once a different palette renders it.
FRAME_STYLE = Style.from_dict(
    {
        "input": "",
        "prompt": "bold #87cefa",
        "tail": "dim",
    }
)


class TranscriptView:
    """Owns the bottom of the terminal: a live tail and the input line.

    Finished output goes to the real scrollback through ``emit``, so it survives
    the session and every terminal feature applies to it. Only the live tail and
    the input are repainted.
    """

    def __init__(
        self,
        *,
        transcript: Transcript | None = None,
        pacer: StreamPacer | None = None,
        clock: Callable[[], float] = time.monotonic,
        write: Callable[[str], None] = print,
    ) -> None:
        self.transcript = transcript if transcript is not None else Transcript()
        self.pacer = pacer if pacer is not None else StreamPacer()
        self._clock = clock
        self._write = write
        self._draft: list[str] = []
        self._app: Application[str] | None = None

        self._buffer = Buffer(
            multiline=True,
            accept_handler=self._on_accept,
            name="input",
        )
        self._app = self._build()

    # -- construction ----------------------------------------------------

    def _build(self) -> Application[str]:
        bindings = KeyBindings()

        @bindings.add("enter")
        def submit(event: object) -> None:
            self._buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def newline(event: object) -> None:
            self._buffer.insert_text("\n")

        @bindings.add("c-c")
        def interrupt(event: object) -> None:
            # prompt_toolkit installs a no-op SIGINT handler for an Application,
            # so Ctrl-C must be bound explicitly or nothing can be interrupted.
            self._app.exit(exception=KeyboardInterrupt())  # type: ignore[union-attr]

        @bindings.add("c-d")
        def end_of_input(event: object) -> None:
            if not self._buffer.text:
                self._app.exit(exception=EOFError())  # type: ignore[union-attr]

        # The gap the operator asked for, then the input. No screen ownership:
        # this is the whole application, and everything above it is scrollback.
        body = Window(
            BufferControl(buffer=self._buffer, lexer=SimpleLexer("class:input")),
            height=Dimension(min=1, max=8),
            style="class:input",
            dont_extend_height=True,
        )
        return Application(
            layout=Layout(body, focused_element=self._buffer),
            key_bindings=bindings,
            style=FRAME_STYLE,
            # Inline, not full screen: the terminal keeps its scrollback, native
            # search and copy. See the module docstring for why that outweighs an
            # absolutely pinned input.
            full_screen=False,
            mouse_support=False,
            editing_mode=EditingMode.EMACS,
        )

    # -- input -----------------------------------------------------------

    def _on_accept(self, buffer: Buffer) -> bool:
        text = buffer.text
        if text.strip() and self._app is not None:
            self._app.exit(result=text)
        buffer.reset()
        return True

    async def read(self) -> str:
        """Run the input line until the operator submits a line."""
        if self._app is None:
            raise RuntimeError("view has no application")
        with patch_stdout(raw=True):
            return await self._app.run_async()

    # -- output ----------------------------------------------------------

    def emit(self, text: str) -> None:
        """Write finished output into the terminal's scrollback.

        This is ordinary output, not a repaint: it scrolls naturally, survives the
        session, and stays selectable.
        """
        self.transcript.append(text)
        for line in text.split("\n"):
            self._write(line)

    def append_streamed(self, text: str) -> None:
        """Add streamed text, repainting only when the frame budget allows."""
        self._draft.append(text)
        if self.pacer.admit(self._clock()):
            self.repaint()

    def settle_streamed(self) -> None:
        """Commit the streaming draft into the scrollback."""
        if not self._draft:
            return
        self.emit("".join(self._draft))
        self._draft.clear()
        self.pacer.reset()

    def repaint(self) -> None:
        if self._app is not None and self._app.is_running:
            self._app.invalidate()

    # -- test seams ------------------------------------------------------

    @property
    def has_pending_input(self) -> bool:
        return bool(self._buffer.text)


def gap_lines() -> int:
    """How many blank rows separate output from the input line."""
    return TRANSCRIPT_GAP_LINES
