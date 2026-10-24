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

from prompt_toolkit import keys
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.lexers import SimpleLexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import HorizontalLine
from rich.console import Console

from cairn.runtime.turn.steering import PendingMessage, SteerQueue
from cairn.terminal.live import LiveRegion
from cairn.terminal.pacing import StreamPacer
from cairn.terminal.palette import Palette
from cairn.terminal.paste import PasteRegistry, decide_paste
from cairn.terminal.queue_view import QueueView
from cairn.terminal.submission import Delivery, LineQueue, PendingLine
from cairn.terminal.transcript import TRANSCRIPT_GAP_LINES, Transcript

#: Rows the live tail may occupy. Bounded rather than weighted: an inline
#: application gives a weighted window every spare row, which pushes the input off
#: screen and makes the session look frozen.
LIVE_TAIL_LINES = 3

#: Rows the palette may occupy, plus one for its key hint.
PALETTE_ROWS = 6

#: Rows the status area uses for session facts: what the session talks to, and
#: what it has spent.
STATUS_LINES = 2

#: Rows the queued-message stack may occupy. Bounded for the same reason as the live
#: tail: an inline application gives a tall window rows the input needs.
QUEUE_STACK_LINES = 3

#: Rows the composer grows to before it scrolls internally. Kept small so the
#: transcript keeps most of the screen; the shipped agents use 10 (Gemini) or
#: two-thirds of the screen (Codex), which suits a full-screen layout but not an
#: inline one.
COMPOSER_MAX_LINES = 4

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
        # Set while a turn runs, so the two keys can mean guide/queue instead of
        # submit; and outside a turn so the hint does not claim otherwise.
        self._running = False
        # The queued messages and which one the operator has selected.
        self._pending = SteerQueue()
        self._queue_view = QueueView()
        # The command surface, opened by typing `/` and closed when the text stops
        # looking like a command.
        self._palette = Palette()
        # The session facts, supplied by the display; one entry per line.
        self._status: list[str] = []
        # Payloads behind the paste markers currently in the buffer.
        self._pastes = PasteRegistry()
        self._runner: asyncio.Task[None] | None = None
        self._app: Application[str] | None = None
        # Set by the CLI once the streaming display exists; the key binding is
        # always present so the hint it prints is never a lie.
        self.on_reveal_reasoning: Callable[[], None] | None = None
        #: Asked to stop the running turn; set by the CLI that owns the turn.
        self.on_interrupt: Callable[[], None] | None = None

        self._buffer = Buffer(
            multiline=True,
            accept_handler=self._on_accept,
            on_text_changed=self._on_text_changed,
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

        @bindings.add("tab")
        def queue_for_after(event: object) -> None:
            """Queue the typed line for after the running turn.

            Enter and Tab differ only while a turn runs: Enter guides it, Tab waits
            for it. Outside a turn both submit, because there is nothing to wait for.
            """
            if not self._running:
                return
            self._submit_typed(delivery="queue")

        @bindings.add("up", filter=Condition(lambda: self._palette.open))
        def palette_previous(event: object) -> None:
            self._palette.move(-1)
            self.repaint()

        @bindings.add("down", filter=Condition(lambda: self._palette.open))
        def palette_next(event: object) -> None:
            self._palette.move(1)
            self.repaint()

        @bindings.add("right", filter=Condition(lambda: self._palette.open))
        def palette_descend(event: object) -> None:
            """Enter the chosen level, or complete the command when there is none."""
            if not self._palette.descend():
                self._complete_palette()
            self.repaint()

        @bindings.add("left", filter=Condition(lambda: self._palette.open))
        def palette_ascend(event: object) -> None:
            """Go back one level. At the root there is nowhere to go, so it stays."""
            self._palette.ascend()
            self.repaint()

        @bindings.add("tab", filter=Condition(lambda: self._palette.open))
        def palette_complete(event: object) -> None:
            """Put the chosen command in the composer, ready to run or extend."""
            self._complete_palette()
            self.repaint()

        @bindings.add("escape", filter=Condition(lambda: self._palette.open))
        def palette_close(event: object) -> None:
            """Hide the palette without touching the turn.

            Escape closes a menu; it must never reach the running turn, which is why
            the binding exists only while the palette is open.
            """
            self._palette.close()
            self.repaint()

        @bindings.add("up")
        def select_previous(event: object) -> None:
            """Move the selection up through the queued messages.

            Only when the composer is empty: with text in it the arrow keys belong
            to editing, which is what a reader expects while typing.
            """
            if self._buffer.text:
                self._buffer.history_backward()
                return
            self._queue_view.move_up(self._pending)
            self.repaint()

        @bindings.add("down")
        def select_next(event: object) -> None:
            if self._buffer.text:
                self._buffer.history_forward()
                return
            self._queue_view.move_down(self._pending)
            self.repaint()

        @bindings.add("delete")
        def discard_selected(event: object) -> None:
            """Drop the selected queued message.

            Delete rather than a letter: it is the key a reader already associates
            with removing the thing that is selected, and it cannot be confused with
            typed text because it only acts on an empty composer.
            """
            if self._buffer.text or not self._running:
                return
            if self._queue_view.discard_selected(self._pending):
                self.repaint()

        @bindings.add("enter", filter=~Condition(lambda: bool(self._buffer.text)))
        def promote_selected(event: object) -> None:
            """Turn the selected queued message into a guide for the running turn.

            Reachable only with an empty composer, so it cannot swallow a submission.
            """
            if not self._running:
                return
            if self._queue_view.promote_selected(self._pending):
                self.repaint()

        @bindings.add("c-c")
        def interrupt(event: object) -> None:
            """Interrupt the running turn, or do nothing when there is none.

            Ctrl-C used to exit the application, which ended the session when the
            operator meant to stop one turn. It now stops the turn: prompt_toolkit
            installs a no-op SIGINT handler for an Application, so the key must be
            bound explicitly either way.

            With the composer holding text the key clears it first, because a reader
            reaching for Ctrl-C while typing usually means "not this message".
            """
            if self._buffer.text:
                self._buffer.reset()
                self.repaint()
                return
            if not self._running:
                return
            if self.on_interrupt is not None:
                self.on_interrupt()

        @bindings.add("c-o")
        def reveal_reasoning(event: object) -> None:
            # Expanding reasoning is a keystroke rather than a click: capturing
            # the mouse would cost the native drag-select and copy that this
            # layout exists to keep.
            if self.on_reveal_reasoning is not None:
                self.on_reveal_reasoning()

        @bindings.add(keys.Keys.BracketedPaste)
        def paste(event: object) -> None:
            """Collapse a large paste so it cannot flood the composer.

            The payload is kept in a registry and restored on submit, so the model
            receives the text the operator pasted while the composer stays readable.
            """
            data = event.data  # type: ignore[attr-defined]
            decision = decide_paste(data, existing=self._pastes.count)
            if not decision.collapsed:
                event.current_buffer.insert_text(decision.payload)  # type: ignore[attr-defined]
                return
            self._pastes.remember(decision.marker, decision.payload)
            event.current_buffer.insert_text(decision.marker)  # type: ignore[attr-defined]

        @bindings.add("c-d")
        def end_of_input(event: object) -> None:
            if not self._buffer.text:
                self._app.exit(exception=EOFError())  # type: ignore[union-attr]

        # Top to bottom: the live tail, a blank separator, the input bracketed by
        # two horizontal rules. The rules have no side segments on purpose: a full
        # box double-draws against the terminal edge and has to be re-measured on
        # every resize, while a rule simply spans the current width. Two of the
        # shipped agents (Claude Code, Gemini CLI) use exactly this shape.
        body = HSplit(
            [
                # A bounded live tail, not a weighted pane. A weighted window
                # claims every spare row of an inline application, which pushes the
                # input line off the visible area — the input is then unreachable
                # and the session looks frozen.
                Window(
                    FormattedTextControl(text=self._live_control),
                    height=Dimension(min=0, max=LIVE_TAIL_LINES),
                    wrap_lines=False,
                    style="class:live",
                    dont_extend_height=True,
                ),
                Window(height=TRANSCRIPT_GAP_LINES),
                # The queued messages, oldest first, above the composer. Shown as a
                # stack so a message typed for later stays visible: one that is
                # accepted and then forgotten is indistinguishable from a dropped
                # message, and the operator would type it again.
                Window(
                    FormattedTextControl(text=self._queue_control),
                    height=Dimension(min=0, max=QUEUE_STACK_LINES),
                    wrap_lines=False,
                    style="class:queued",
                    dont_extend_height=True,
                ),
                HorizontalLine(),
                Window(
                    FormattedTextControl(text=self._palette_control),
                    # Bounded like every other region: a taller window would claim
                    # rows the transcript and the composer need.
                    height=Dimension(min=0, max=PALETTE_ROWS + 1),
                    wrap_lines=False,
                    style="class:palette",
                    dont_extend_height=True,
                ),
                Window(
                    BufferControl(
                        buffer=self._buffer, lexer=SimpleLexer("class:input")
                    ),
                    # Four lines is the readable size at the bottom of a terminal:
                    # enough for a pasted sentence or a short list, small enough to
                    # leave the transcript most of the screen. Beyond it the buffer
                    # scrolls internally so the transcript never jumps.
                    height=Dimension(min=1, max=COMPOSER_MAX_LINES),
                    style="class:input",
                    dont_extend_height=True,
                ),
                # The hint names what the two keys mean right now, only while a
                # turn runs: outside one both simply submit.
                Window(
                    FormattedTextControl(text=self._hint_control),
                    # Two rows for the session facts, plus one for the key hint while
                    # a turn runs. Bounded like every other region: an inline
                    # application gives a taller window rows the input needs.
                    height=Dimension(min=0, max=STATUS_LINES + 1),
                    style="class:hint",
                    dont_extend_height=True,
                ),
                HorizontalLine(),
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

    def set_running(self, running: bool) -> None:
        """Record whether a turn is in flight.

        The composer's two keys mean different things in the two states, and the
        hint must not promise a behaviour that is not available.
        """
        self._running = running
        self.repaint()

    def observe_status(self, text: str) -> None:
        """Adopt the session facts the status area shows.

        The display owns what these facts are; the frame only decides where they
        appear, which is under the input where they are read while waiting. Two
        lines are kept apart because they answer different questions: what the
        session talks to, and what it has spent.
        """
        self._status = [line for line in text.split("\n") if line.strip()]
        self.repaint()

    def _status_text(self) -> str:
        return "\n".join(self._status)

    def available_width(self) -> int | None:
        """The columns this frame has, for callers that must fit their text."""
        return self._available_width()

    def _available_width(self) -> int | None:
        """The columns the status may use, when the terminal size is known."""
        if self._app is None:
            return None
        try:
            return self._app.output.get_size().columns
        except (AttributeError, ValueError):
            return None

    def _complete_palette(self) -> None:
        """Copy the palette's choice into the composer.

        The full command line is used, so a chosen sub-command becomes
        ``/model use`` rather than the level's bare name.
        """
        command = self._palette.command_line()
        if not command:
            return
        self._buffer.reset()
        self._buffer.insert_text(command + " ")

    def _on_text_changed(self, buffer: Buffer) -> None:
        """Keep the command surface in step with what is typed."""
        self._palette.update(buffer.text)
        self.repaint()

    def _palette_control(self) -> ANSI:
        """The command list, shown only while a command is being typed."""
        if not self._palette.open:
            return ANSI("")
        lines = []
        for row in self._palette.rows():
            marker = ">" if row.selected else " "
            lines.append(f"{marker} {row.name:<10} {row.description}")
        hint = self._palette.hint()
        if hint:
            lines.append(f"  {hint}")
        return ANSI("\n".join(lines))

    def _queue_control(self) -> ANSI:
        """The queued messages, with the selected one marked."""
        rows = self._queue_view.rows(self._pending)
        if not rows:
            return ANSI("")
        lines = []
        for row in rows:
            marker = ">" if row.selected else " "
            text = row.text.replace("\n", " ")
            if len(text) > 60:
                text = text[:57] + "..."
            lines.append(f"{marker} {text}")
        return ANSI("\n".join(lines))

    def _hint_control(self) -> ANSI:
        """The session facts and what the keys mean, on one dim line.

        Model, usage and rate live below the input because they describe the
        session rather than the answer: they are read while waiting, and putting
        them in the transcript would add a line per measurement. The key hint joins
        them only while a turn runs, since outside one both keys simply submit.
        """
        parts = [
            part
            for part in (
                self._status_text(),
                self._queue_view.hint(running=self._running),
            )
            if part
        ]
        return ANSI(f"\x1b[2m{'  ·  '.join(parts)}\x1b[0m" if parts else "")

    def _live_control(self) -> ANSI:
        """The live region as ANSI, so its dim styling survives the frame."""
        return ANSI(self._live_text)

    def _submit_typed(self, *, delivery: Delivery) -> None:
        """Submit the composer's text, restoring pasted payloads first."""
        text = self._buffer.text
        self._buffer.reset()
        expanded = self._pastes.expand(text)
        self._pastes.clear()
        if not expanded.strip():
            return
        if delivery == "queue":
            # A queued message waits for the turn, so it lives in the queue the
            # stack above the composer renders from.
            self._pending.push(PendingMessage(text=expanded, delivery="queue"))
        self._lines.submit(PendingLine(text=expanded, delivery=delivery))
        self.repaint()

    def _on_accept(self, buffer: Buffer) -> bool:
        """Deliver the line and keep the input running.

        The application is not exited: it is the session's one terminal owner, so
        exiting would end the input line the turn loop still needs.
        """
        # Enter means: start work when idle, guide the running turn when busy. The
        # same key does the obvious thing in both states, which is why it needs no
        # modifier to remember.
        self._submit_typed(delivery="steer" if self._running else "now")
        return True

    async def read(self) -> PendingLine:
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
        # Only the last few rows are kept: the pane is a bounded window, and text
        # longer than it would either be clipped or push the layout around. The
        # tail is what a reader watches; earlier lines are already in the
        # scrollback or in the reasoning block.
        lines = text.split("\n")
        if len(lines) > LIVE_TAIL_LINES:
            lines = lines[-LIVE_TAIL_LINES:]
        self._live_text = "\n".join(lines)
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
