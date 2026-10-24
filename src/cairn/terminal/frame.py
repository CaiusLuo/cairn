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

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from io import StringIO

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console

from cairn.terminal.live import LiveRegion
from cairn.terminal.pacing import StreamPacer
from cairn.terminal.submission import LineQueue
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
        self._live_text = ""
        # Submitted lines go through a queue because the input line runs for the
        # whole session: the turn and the input overlap, they are not nested.
        self._lines = LineQueue()
        self._runner: asyncio.Task[None] | None = None
        self._app: Application[str] | None = None
        # Set by the CLI once the streaming display exists; the key binding is
        # always present so the hint it prints is never a lie.
        self.on_reveal_reasoning: Callable[[], None] | None = None

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

        @bindings.add("c-o")
        def reveal_reasoning(event: object) -> None:
            # Expanding reasoning is a keystroke rather than a click: capturing
            # the mouse would cost the native drag-select and copy that this
            # layout exists to keep.
            if self.on_reveal_reasoning is not None:
                self.on_reveal_reasoning()

        @bindings.add("c-d")
        def end_of_input(event: object) -> None:
            if not self._buffer.text:
                self._app.exit(exception=EOFError())  # type: ignore[union-attr]

        # Top to bottom: the live region, the gap the operator asked for, then the
        # input. No screen ownership: everything above this block is the terminal's
        # own scrollback.
        body = HSplit(
            [
                Window(
                    FormattedTextControl(text=self._live_control),
                    height=Dimension(weight=1, min=0),
                    wrap_lines=False,
                    style="class:live",
                    dont_extend_height=True,
                ),
                Window(height=TRANSCRIPT_GAP_LINES),
                Window(
                    BufferControl(
                        buffer=self._buffer, lexer=SimpleLexer("class:input")
                    ),
                    height=Dimension(min=1, max=8),
                    style="class:input",
                    dont_extend_height=True,
                ),
            ]
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

    def _live_control(self) -> ANSI:
        """The live region as ANSI, so its dim styling survives the frame."""
        return ANSI(self._live_text)

    def _on_accept(self, buffer: Buffer) -> bool:
        """Deliver the line and keep the input running.

        The application is not exited: it is the session's one terminal owner, so
        exiting would end the input line the turn loop still needs.
        """
        text = buffer.text
        buffer.reset()
        if text.strip():
            self._lines.submit(text)
        return True

    async def read(self) -> str:
        """Wait for the next submitted line, running the input line meanwhile.

        The application is started once and left running: it owns the terminal for
        the session, so starting it per message would mean tearing down and
        rebuilding the input line between every turn. A submitted line arrives
        through the queue instead.
        """
        await self._ensure_running()
        return await self._lines.next_line()

    async def _ensure_running(self) -> None:
        """Start the input line if it is not already running."""
        if self._app is None:
            raise RuntimeError("view has no application")
        if self._runner is not None and not self._runner.done():
            return
        self._runner = asyncio.create_task(self._run_application())
        # Let the first frame paint before the caller starts waiting for input,
        # so a prompt is visible even when nothing else has been printed.
        await asyncio.sleep(0)

    async def _run_application(self) -> None:
        with patch_stdout(raw=True):
            await self._app.run_async()  # type: ignore[union-attr]

    async def aclose(self) -> None:
        """Stop the input line and release anything waiting on it."""
        self._lines.close()
        if self._app is not None and self._app.is_running:
            # No result: the session is ending, not submitting a line.
            self._app.exit(exception=EOFError())
        if self._runner is not None:
            # Ending the session is expected here, not an error to report.
            with suppress(KeyboardInterrupt, EOFError):
                await self._runner
            self._runner = None

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

    def show_live(self, text: str) -> None:
        """Set the live region shown above the input line.

        This is the only part of the screen the application repaints, which is why
        it is separated from ``emit``: finished output belongs in the scrollback and
        must never be redrawn.
        """
        self._live_text = text
        self.repaint()

    @property
    def live_text(self) -> str:
        return self._live_text

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


class FrameLiveRegion:
    """A live region drawn inside the frame's own bottom viewport.

    This is the host that lets the input keep its position: the region paints above
    the input line inside the application, so the two never fight for the cursor.
    A ``rich.Live`` region cannot do this — it moves the cursor itself, and an
    application repaints over it.

    The region renders each frame to text and hands it to the view, which owns the
    repaint schedule. Rendering here rather than in the view keeps the styling
    rules — dim reasoning, foreground-only colour — in the one module that already
    states them.
    """

    def __init__(self, view: TranscriptView, *, width: int = 80) -> None:
        self._view = view
        self._width = width
        self._renderable: object = None

    def start(self, renderable: object) -> None:
        self._renderable = renderable
        self._paint()

    def update(self, renderable: object) -> None:
        self._renderable = renderable
        self._paint()

    def stop(self) -> None:
        self._renderable = None
        self._view.show_live("")

    def _paint(self) -> None:
        self._view.show_live(_render_to_text(self._renderable, width=self._width))


def _render_to_text(renderable: object, *, width: int) -> str:
    """Render one rich renderable to plain ANSI text.

    A fresh console per frame keeps this independent of the global one: the live
    region must not inherit whatever the scrollback path configured.
    """
    if renderable is None:
        return ""
    buffer = StringIO()
    renderer = Console(
        file=buffer,
        width=width,
        color_system="256",
        force_terminal=True,
        legacy_windows=False,
    )
    renderer.print(renderable)
    return buffer.getvalue().rstrip("\n")


def frame_live_region(view: TranscriptView) -> "Callable[[object], LiveRegion]":
    """A live-region factory that paints inside ``view``."""

    def factory(renderable: object) -> LiveRegion:
        return FrameLiveRegion(view)

    return factory
