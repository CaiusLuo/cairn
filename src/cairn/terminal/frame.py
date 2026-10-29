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
from collections.abc import Callable, Sequence
from contextlib import suppress
from io import StringIO

from prompt_toolkit import keys
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import HorizontalLine
from rich.console import Console

from cairn.runtime.turn.steering import PendingMessage, SteerQueue
from cairn.terminal.action import Action, Tone
from cairn.terminal.live import LiveRegion
from cairn.terminal.pacing import StreamPacer
from cairn.terminal.palette import Palette
from cairn.terminal.paste import PasteRegistry, decide_paste
from cairn.terminal.queue_view import QueueView
from cairn.terminal.selection import SelectionTokenLexer, parse_selection_tokens
from cairn.terminal.submission import Delivery, LineQueue, PendingLine
from cairn.terminal.transcript import TRANSCRIPT_GAP_LINES, Transcript

#: Rows the activity slot may occupy. Bounded rather than weighted: an inline
#: application gives a weighted window every spare row, which pushes the input off
#: screen and makes the session look frozen.
#:
#: Three is what the surface actually needs: one row naming the activity (a tool, or
#: the thinking header) and two rows of preview under it. It is a slot rather than a
#: live tail: thinking, tool work and an interrupt each replace the last, so a longer
#: turn never makes it taller.
ACTIVITY_ROWS = 3

#: Rows the palette may occupy, plus one for its key hint.
PALETTE_ROWS = 6

#: Rows the action surface may occupy: a correction, or a chooser with its decision
#: facts and its key hint. Bounded so it can never push the composer or the status
#: off screen, but large enough that a provider's whole decision — endpoint,
#: credential variable, models, both rows and the key hint — is readable without
#: scrolling, because a fact the operator cannot see is a fact they cannot weigh.
ACTION_ROWS = 14

#: Columns assumed for the action surface before the terminal has reported a size.
ACTION_DEFAULT_WIDTH = 80


def _wrap_cells(text: str, width: int) -> list[str]:
    """Split ``text`` into pieces of at most ``width`` display cells.

    Recut by display cells rather than characters, so a CJK endpoint or a path with
    wide glyphs wraps where the terminal would wrap it and no row overflows the frame.
    """
    pieces: list[str] = []
    current: list[str] = []
    used = 0
    for char in text:
        cells = get_cwidth(char)
        if cells < 0:
            current.append(char)
            continue
        if used + cells > width and current:
            pieces.append("".join(current))
            current = []
            used = 0
        current.append(char)
        used += cells
    pieces.append("".join(current))
    return pieces


#: Rows the status area uses for session facts: what the session talks to, and
#: what it has spent. Exactly two, always, with nothing beneath them.
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
        "live": "dim",
        "palette": "",
        "selection-token": "fg:#87cefa bold",
        "queued": "",
        "status": "",
        "action": "",
        "action-correction": "bold yellow",
        "action-refusal": "bold red",
    }
)


class TranscriptView:
    """Owns the bottom of the terminal: the activity slot and the composer.

    Finished output goes to the real scrollback through ``emit``, so it survives
    the session and every terminal feature applies to it. Only two things are
    repainted here: whatever the turn is doing now, and the input with its two
    status rows.
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
        self._flush_live: Callable[[], bool] | None = None
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
        #: Supplies (root rows, per-command child rows) for the dynamic palette.
        self.on_palette_data: (
            Callable[
                [],
                tuple[
                    Sequence[tuple[str, str]], dict[str, tuple[tuple[str, str], ...]]
                ],
            ]
            | None
        ) = None
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
        #: Told the frame's width, so the display can re-fit its status and preview.
        #: Called on every paint because a resize changes it without warning.
        self.on_width: Callable[[int], None] | None = None
        #: The width last reported, so the callback fires only when it changes.
        self._reported_width: int | None = None
        #: What the operator must see about what they just typed: a correction, a
        #: refusal, or a chooser. Temporary by construction — the next one replaces
        #: it, and the first edit clears it.
        self._action: Action | None = None
        self._action_message_offset = 0
        #: Called when the operator confirms a chooser row, with the row's name.
        self.on_action_choose: Callable[[str], None] | None = None
        #: Called when a chooser's selection moves: ``(from_index, step)``. The
        #: caller rebuilds the chosen row's details and returns the action to show.
        #: The current index is passed because rebuilding an action from scratch
        #: selects its first row, so adding ``step`` to that would pin the cursor at
        #: the top: repeated Down could never reach the third entry.
        self.on_action_move: Callable[[int, int], Action | None] | None = None
        #: Told which command produced the action now on the surface, so a later
        #: Enter knows which chooser it is answering. Empty when the action had no
        #: rows to choose from.
        self.on_action_command: Callable[[str], None] | None = None

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

        # Enter submits the composer, except while a chooser is asking a question:
        # there it confirms the chosen row. Registration order alone cannot express
        # that — this binding is created before the chooser's — so the filter is what
        # keeps a menu from being submitted as text.
        @bindings.add(
            "enter",
            filter=~Condition(lambda: self._action_open() or self._palette.open),
        )
        def submit(event: object) -> None:
            self._buffer.validate_and_handle()

        @bindings.add(
            "escape",
            "enter",
            filter=~Condition(lambda: self._action_open() or self._palette.open),
        )
        def newline(event: object) -> None:
            self._buffer.insert_text("\n")

        @bindings.add(
            "tab",
            filter=Condition(
                lambda: (
                    self._running and not self._action_open() and not self._palette.open
                )
            ),
        )
        def queue_for_after(event: object) -> None:
            """Queue the typed line for after the running turn.

            Enter and Tab differ only while a turn runs: Enter guides it, Tab waits
            for it. Outside a turn both submit, because there is nothing to wait for.
            """
            if not self._running:
                return
            self._submit_typed(delivery="queue")

        # The action surface owns the arrows while it is open, because it is the
        # decision in front of the operator. These bindings come first so they win
        # over the palette and the queue, which would otherwise move underneath a
        # chooser that is asking for an answer.
        @bindings.add("up", filter=Condition(lambda: self._choosable_action()))
        def action_previous(event: object) -> None:
            self._move_action(-1)

        @bindings.add("down", filter=Condition(lambda: self._choosable_action()))
        def action_next(event: object) -> None:
            self._move_action(1)

        @bindings.add("enter", filter=Condition(lambda: self._choosable_action()))
        def action_choose(event: object) -> None:
            """Confirm the chosen row and close the surface."""
            action = self._action
            if action is None:
                return
            row = action.selected_row()
            self._action = None
            self.repaint()
            if row is not None and self.on_action_choose is not None:
                self.on_action_choose(row.name)

        @bindings.add("escape", filter=Condition(lambda: self._action is not None))
        def action_close(event: object) -> None:
            """Escape declines: it closes the chooser and changes nothing.

            Declining must never reach the running turn, which is why this binding
            exists only while an action is on screen.
            """
            self._dismiss_action()

        @bindings.add("pageup", filter=Condition(lambda: self._action_open()))
        def action_facts_previous(event: object) -> None:
            self._scroll_action_facts(-1)

        @bindings.add("pagedown", filter=Condition(lambda: self._action_open()))
        def action_facts_next(event: object) -> None:
            self._scroll_action_facts(1)

        @bindings.add("up", filter=Condition(lambda: self._palette_focused()))
        def palette_previous(event: object) -> None:
            self._palette.move(-1)
            self.repaint()

        @bindings.add("down", filter=Condition(lambda: self._palette_focused()))
        def palette_next(event: object) -> None:
            self._palette.move(1)
            self.repaint()

        @bindings.add("right", filter=Condition(lambda: self._palette_focused()))
        def palette_descend(event: object) -> None:
            """Open the chosen entry's next level, and nothing more.

            Right navigates, so it never runs a command: on an entry with no level it
            does not act at all.
            """
            self._palette.descend()
            self.repaint()

        @bindings.add("enter", filter=Condition(lambda: self._palette_focused()))
        def palette_run(event: object) -> None:
            """Run the chosen entry: open its level, or submit the complete command.

            Enter has to mean "do the thing", the same as it does in the composer.
            Completing the line and stopping there made the first Enter arrive
            nowhere: ``/exit`` was put in the composer with a trailing space, not
            submitted, so ending the session took a second press. A choice that
            opens a level still only opens it, because the level chooses the rest of
            the command line — and a running turn is steered or queued by the same
            rule the composer uses, never exited into.

            A command the list does not carry, such as a typed path the catalogue
            has no row for, is submitted exactly as typed: the palette is a way to
            type a command, not a filter on which ones count.
            """
            if self._palette.descend():
                self.repaint()
                return
            token = self._palette.insertion()
            if token:
                self._insert_palette_token(token)
                return
            typed = self._buffer.text.strip()
            if len(typed.split(maxsplit=1)) < 2:
                # A palette choice may be more complete than the composer text:
                # submit the selected command, including any navigated submenu.
                self._complete_palette()
            self._submit_typed(delivery="steer" if self._running else "now")

        @bindings.add("left", filter=Condition(lambda: self._palette_focused()))
        def palette_ascend(event: object) -> None:
            """Go back one level. At the root there is nowhere to go, so it stays."""
            self._palette.ascend()
            self.repaint()

        @bindings.add("tab", filter=Condition(lambda: self._palette_focused()))
        def palette_complete(event: object) -> None:
            """Put the chosen command or selection in the composer."""
            token = self._palette.insertion()
            if token:
                self._insert_palette_token(token)
                return
            self._complete_palette()
            self.repaint()

        @bindings.add("escape", filter=Condition(lambda: self._palette_focused()))
        def palette_close(event: object) -> None:
            """Hide the palette without touching the turn.

            Escape closes a menu; it must never reach the running turn, which is why
            the binding exists only while the palette is open.
            """
            self._palette.close()
            self.repaint()

        @bindings.add(
            "up",
            filter=~Condition(lambda: self._action_open() or self._palette.open),
        )
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

        @bindings.add(
            "down",
            filter=~Condition(lambda: self._action_open() or self._palette.open),
        )
        def select_next(event: object) -> None:
            if self._buffer.text:
                self._buffer.history_forward()
                return
            self._queue_view.move_down(self._pending)
            self.repaint()

        @bindings.add(
            "delete",
            filter=~Condition(lambda: self._action_open() or self._palette.open),
        )
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

        @bindings.add(
            "enter",
            filter=~Condition(
                lambda: (
                    bool(self._buffer.text) or self._action_open() or self._palette.open
                )
            ),
        )
        def promote_selected(event: object) -> None:
            """Turn the selected queued message into a guide for the running turn.

            Reachable only with an empty composer, so it cannot swallow a submission.
            """
            if not self._running:
                return
            queued = self._pending.queued()
            if self._queue_view.selected < len(queued):
                selection = parse_selection_tokens(
                    queued[self._queue_view.selected].text
                )
                if selection.skills or selection.mcp_servers:
                    self.show_action(
                        Action(
                            message="Extension selection stays queued for the next turn."
                        )
                    )
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

            The key unwinds one layer at a time, so a reader who presses it while a
            menu is open closes the menu rather than killing the work behind it:

            1. a chooser or correction is showing — decline it, change nothing;
            2. the palette is open — close it, leave the turn alone;
            3. the composer holds text — clear it, which is what a reader reaching
               for Ctrl-C while typing usually means;
            4. a turn is running — stop it, and only then.
            """
            if self._action is not None:
                self._dismiss_action()
                return
            if self._palette.open:
                self._palette.close()
                self.repaint()
                return
            if self._buffer.text:
                self._buffer.reset()
                self.repaint()
                return
            if not self._running:
                return
            if self.on_interrupt is not None:
                self.on_interrupt()

        @bindings.add("c-o", filter=~Condition(lambda: self._action_open()))
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
                if self._action is not None:
                    self._dismiss_action()
                self._app.exit(exception=EOFError())  # type: ignore[union-attr]

        # Top to bottom, one viewport owner:
        #
        #   activity slot      what the turn is doing now (thinking/tool/interrupt)
        #   action slot        a correction, a refusal, or a chooser
        #   palette            commands, while a command is being typed
        #   queue stack        messages typed for after the running turn
        #   rule               the composer opens
        #   composer           the editable line
        #   rule               the composer closes
        #   status             exactly two rows: identity, then numbers
        #
        # The rules have no side segments on purpose: a full box double-draws against
        # the terminal edge and has to be re-measured on every resize, while a rule
        # simply spans the current width. Two of the shipped agents (Claude Code,
        # Gemini CLI) use exactly this shape.
        #
        # Nothing but the two rules touches the composer: the palette and the action
        # surface are decisions, so they sit above it rather than between the
        # composer's rules and the text being edited, which was what made typing `/`
        # look like the input had moved. The key hints belong to whichever temporary
        # surface is showing them, so they ride inside the activity, action or palette
        # rows instead of costing a third permanent row under the status.
        body = HSplit(
            [
                # The activity slot: thinking, tool work and an interrupt all render
                # here, in this one place, replacing each other.
                Window(
                    FormattedTextControl(text=self._live_control),
                    height=lambda: Dimension.exact(self._live_rows()),
                    wrap_lines=False,
                    style="class:live",
                    dont_extend_height=True,
                ),
                # The action slot: what the operator must see about what they just
                # typed. It takes priority over the palette because it is a decision
                # or a correction, and over the activity because those are both
                # present tense about *this* session rather than about the turn.
                Window(
                    FormattedTextControl(text=self._action_control),
                    # The window must be able to show everything the control draws.
                    # With `dont_extend_height` and a max-only height, an inline
                    # layout squeezed this to a row or two and the decision's rows —
                    # the ways to accept or decline — were never painted.
                    height=lambda: Dimension.exact(
                        ACTION_ROWS if self._action is not None else 0
                    ),
                    wrap_lines=False,
                    style="class:action",
                    dont_extend_height=True,
                ),
                # The palette is an action too, so it shares the place above the
                # composer rather than sitting inside the composer.
                Window(
                    FormattedTextControl(text=self._palette_control),
                    height=lambda: Dimension.exact(
                        PALETTE_ROWS + 1 if self._palette_focused() else 0
                    ),
                    wrap_lines=False,
                    style="class:palette",
                    dont_extend_height=True,
                ),
                Window(height=TRANSCRIPT_GAP_LINES),
                # The queued messages, oldest first, above the composer. Shown as a
                # stack so a message typed for later stays visible: one that is
                # accepted and then forgotten is indistinguishable from a dropped
                # message, and the operator would type it again.
                Window(
                    FormattedTextControl(text=self._queue_control),
                    height=lambda: Dimension.exact(
                        min(QUEUE_STACK_LINES, self._queue_rows())
                    ),
                    wrap_lines=False,
                    style="class:queued",
                    dont_extend_height=True,
                ),
                HorizontalLine(),
                Window(
                    BufferControl(buffer=self._buffer, lexer=SelectionTokenLexer()),
                    # Four lines is the readable size at the bottom of a terminal:
                    # enough for a pasted sentence or a short list, small enough to
                    # leave the transcript most of the screen. Beyond it the buffer
                    # scrolls internally so the transcript never jumps.
                    height=Dimension(min=1, max=COMPOSER_MAX_LINES),
                    style="class:input",
                    dont_extend_height=True,
                ),
                HorizontalLine(),
                # Exactly two rows, and nothing beneath them: they are the last thing
                # on screen so a reader's eye lands there while waiting, and a third
                # row would either duplicate them or turn into a permanent hint bar.
                Window(
                    FormattedTextControl(text=self._status_control),
                    height=Dimension.exact(STATUS_LINES),
                    wrap_lines=False,
                    style="class:status",
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

    @property
    def steering(self) -> SteerQueue:
        """The single pending-message queue shared with the running turn."""
        return self._pending

    def set_running(self, running: bool) -> None:
        """Record whether a turn is in flight.

        The composer's two keys mean different things in the two states, and the
        hint must not promise a behaviour that is not available.
        """
        self._running = running
        if not running:
            for item in self._pending.drain():
                self._lines.submit(PendingLine(text=item.text))
        self.repaint()

    def show_action(self, action: Action | None) -> None:
        """Put an action on the surface, or clear it with ``None``.

        One owner for what is showing: the next action replaces the last, so this
        surface cannot accumulate into a transcript of corrections.

        A question of authority takes the space the activity slot would use. Both
        live above the composer, and on a short terminal the turn's progress is not
        worth pushing the decision — the endpoint, the rows, the way to decline —
        below the fold. The activity returns as soon as the question is answered.
        """
        self._action = action.clamped() if action is not None else None
        self._action_message_offset = 0
        if self._action is not None and self._action.decides_authority:
            self._live_text = ""
        self.repaint()

    @property
    def action(self) -> Action | None:
        """What the surface is currently showing, if anything."""
        return self._action

    def _choosable_action(self) -> bool:
        """Whether an open action has rows to move between."""
        return self._action is not None and self._action.choosable

    def _action_open(self) -> bool:
        return self._action is not None

    def _palette_focused(self) -> bool:
        return self._palette.open and not self._action_open()

    def _dismiss_action(self) -> None:
        """Close the visible action and tell its owner it was declined."""
        if self._action is None:
            return
        self._action = None
        self._action_message_offset = 0
        if self.on_action_command is not None:
            self.on_action_command("")
        self.repaint()

    def _scroll_action_facts(self, step: int) -> None:
        action = self._action
        if action is None:
            return
        width = self._available_width() or ACTION_DEFAULT_WIDTH
        message_lines = self._wrapped_message(action, width)
        row_budget = min(len(action.rows), 4)
        hint_rows = 1 if action.hint or len(message_lines) > 1 else 0
        message_budget = max(1, ACTION_ROWS - row_budget - hint_rows)
        self._action_message_offset = max(
            0,
            min(
                self._action_message_offset + step * max(1, message_budget - 2),
                max(0, len(message_lines) - message_budget + 1),
            ),
        )
        self.repaint()

    def _move_action(self, step: int) -> None:
        """Move the chooser's selection, re-expanding the chosen row's details.

        The move starts from the row the operator is looking at, not from zero: the
        caller rebuilds the action when it needs fresh details, and a rebuild always
        selects the first row, so the current index has to be carried across.
        """
        action = self._action
        if action is None or not action.rows:
            return
        current = action.clamped().selected
        if self.on_action_move is not None:
            moved = self.on_action_move(current, step)
            if moved is not None:
                self._action = moved.clamped()
                self.repaint()
                return
        action.selected = current + step
        action.clamped()
        self.repaint()

    def observe_status(self, text: str) -> None:
        """Adopt the session facts the status area shows.

        The display owns what these facts are; the frame only decides where they
        appear, which is under the input where they are read while waiting. Two
        lines are kept apart because they answer different questions: what the
        session talks to, and what it has spent.

        Only ``STATUS_LINES`` rows are kept. Trimming here rather than at render time
        makes the two-row guarantee a property of the state, so no later caller can
        grow the footer into a third row by publishing one.
        """
        rows = [line for line in text.split("\n") if line.strip()]
        self._status = rows[:STATUS_LINES]
        if not self.flush_live():
            self.repaint()

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

    def _report_width(self) -> None:
        """Tell the display the width, when it has changed since the last paint.

        This runs from the status control, which every paint reaches, so a resize is
        noticed without a resize event to subscribe to. Reporting only on a change
        keeps the display from re-fitting on every keystroke.
        """
        width = self._available_width()
        if width is None or width == self._reported_width:
            return
        self._reported_width = width
        if self.on_width is not None:
            self.on_width(width)
        self.flush_live()

    def _insert_palette_token(self, token: str) -> None:
        """Replace the active slash command with a selection token."""
        current = self._buffer.text
        offset = self._palette.command_offset()
        prefix = current[:offset]
        suffix = current[self._buffer.cursor_position :]
        self._buffer.document = Document(
            prefix + token + " " + suffix, cursor_position=len(prefix + token + " ")
        )
        self._palette.close()
        self.repaint()

    def _complete_palette(self) -> None:
        """Copy the palette's choice into the composer, ready to run or extend.

        The full command line is used, so a chosen sub-command becomes
        ``/model use`` rather than the level's bare name. No trailing space is
        added: a complete command is complete, and the space made ``/exit`` look
        like an argument was still expected.
        """
        command = self._palette.command_line()
        if not command:
            return
        self._buffer.reset()
        self._buffer.insert_text(command)
        # Inserting the completion fires `_on_text_changed`, which normally opens
        # the palette for slash-prefixed text. Completing hands focus back to the
        # composer; the next Enter submits the command.
        self._palette.close()
        self.repaint()

    def _on_text_changed(self, buffer: Buffer) -> None:
        """Keep the command surface in step with what is typed.

        The first edit clears a correction: it was about the line the operator has
        already started replacing, and leaving it up would describe a message that no
        longer exists. A chooser is left alone, because typing while choosing is
        ordinary.
        """
        if self._action is not None and self._action.tone is Tone.CORRECTION:
            self._action = None
        if self.on_palette_data is not None:
            root, children = self.on_palette_data()
            self._palette.set_dynamic(root, children)
        self._palette.update(buffer.document.text_before_cursor)
        self.repaint()

    def _status_control(self) -> ANSI:
        """The two session rows under the composer, and nothing else.

        They answer different questions and age differently: what this session talks
        to changes when the operator switches, while the numbers change every second.
        Keeping them as exactly two lines is what makes them scannable, and is why no
        key hint, queue state or blank spacer shares the space — a third row would
        either duplicate the facts or become a permanent bar of instructions nobody
        reads after the first hour.
        """
        lines = self._status[:STATUS_LINES]
        # Every paint passes through here, which is what makes a resize noticed
        # without a separate event to subscribe to.
        self._report_width()
        if not lines:
            # The display has not published anything yet. Two blank rows still keep
            # the composer's position stable; nothing else may claim them.
            return ANSI("\n" * (STATUS_LINES - 1))
        return ANSI("\n".join(lines))

    def _action_control(self) -> ANSI:
        """The action surface: a correction, a refusal, a chooser or a question.

        Emphasis follows the tone, because a correction and a refusal are the two the
        operator must not miss. Rows carry a marker for the chosen one and a note for
        the current value, so a chooser shows what is already in use without the
        operator having to remember it.

        The message is wrapped to the frame's width rather than clipped. A provider
        decision states an endpoint in full precisely because a shortened one hides
        the host, so cutting the line at the margin would undo the reason it is
        shown. A list taller than the window scrolls to keep the cursor on screen.
        """
        action = self._action
        if action is None:
            return ANSI("")
        style = {
            Tone.CORRECTION: "\x1b[1;33m",
            Tone.REFUSAL: "\x1b[1;31m",
            Tone.INFO: "\x1b[2m",
        }[action.tone]
        width = self._available_width() or ACTION_DEFAULT_WIDTH
        message_lines = self._wrapped_message(action, width)
        row_budget = min(len(action.rows), 4)
        hint_rows = 1 if action.hint or len(message_lines) > 1 else 0
        message_budget = max(1, ACTION_ROWS - row_budget - hint_rows)
        max_offset = max(0, len(message_lines) - message_budget + 1)
        self._action_message_offset = min(self._action_message_offset, max_offset)
        start = self._action_message_offset
        has_previous = start > 0
        has_next = start + message_budget < len(message_lines)
        fact_budget = max(1, message_budget - int(has_previous) - int(has_next))
        visible_message = message_lines[start : start + fact_budget]
        lines: list[str] = []
        if start:
            lines.append("\x1b[2m↑ more facts (PgUp)\x1b[0m")
        lines.extend(f"{style}{piece}\x1b[0m" for piece in visible_message)
        if has_next:
            lines.append("\x1b[2m↓ more facts (PgDn)\x1b[0m")
        for line in self._visible_rows(action, width, row_budget):
            lines.append(line)
        if action.hint:
            for piece in _wrap_cells(f"  {action.hint}", width):
                lines.append(f"\x1b[2m{piece}\x1b[0m")
        elif len(message_lines) > message_budget:
            lines.append("\x1b[2m  PgUp/PgDn read decision facts\x1b[0m")
        return ANSI("\n".join(lines))

    @staticmethod
    def _wrapped_message(action: Action, width: int) -> list[str]:
        return [
            piece
            for text in action.message.split("\n")
            for piece in (_wrap_cells(text, width) or [""])
        ]

    def _visible_rows(self, action: Action, width: int, budget: int) -> list[str]:
        """The row lines to draw, scrolled so the chosen row is always visible.

        A list longer than the window used to leave the cursor below the visible
        area, so Enter would confirm a row the operator could not see. The window
        slides just enough to keep the selection on screen.
        """
        rendered = [
            f"  {('>' if index == action.selected else ' ')} {row.name}"
            f"{f'  {row.description}' if row.description else ''}"
            f"{'   current' if row.current else ''}"
            for index, row in enumerate(action.rows)
        ]
        if not rendered:
            return []
        # Reserve the message viewport and hint before showing rows.
        budget = max(1, budget)
        if len(rendered) <= budget:
            start = 0
        else:
            selected = min(action.selected, len(rendered) - 1)
            start = max(0, min(selected - budget + 1, len(rendered) - budget))
        out: list[str] = []
        for offset, line in enumerate(rendered[start : start + budget]):
            index = start + offset
            lead = "\x1b[1;36m" if index == action.selected else "\x1b[2m"
            for piece in _wrap_cells(line, width) or [""]:
                out.append(f"{lead}{piece}\x1b[0m")
        return out

    def _palette_control(self) -> ANSI:
        """The command list, shown only while a command is being typed.

        The key hints ride here, inside the palette, rather than in a permanent row:
        they describe keys that only mean something while this surface is open, and
        they disappear with it.
        """
        if not self._palette_focused():
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
        """The queued messages, with the selected one marked.

        The steering hint belongs to this stack rather than to a permanent footer:
        it explains keys that only differ from ordinary typing while a turn runs.
        """
        rows = self._queue_view.rows(self._pending)
        hint = self._queue_view.hint(running=self._running)
        if not rows and not hint:
            return ANSI("")
        lines = []
        for row in rows:
            marker = ">" if row.selected else " "
            text = row.text.replace("\n", " ")
            if len(text) > 60:
                text = text[:57] + "..."
            lines.append(f"{marker} {text}")
        if hint:
            lines.append(f"\x1b[2m  {hint}\x1b[0m")
        return ANSI("\n".join(lines))

    def _live_control(self) -> ANSI:
        """The live content, keeping its start visible if it exceeds the viewport."""
        lines = self._live_text.splitlines()
        limit = self._live_row_limit()
        if len(lines) > limit:
            lines = [*lines[: max(0, limit - 1)], "  … answer continues"]
        return ANSI("\n".join(lines))

    def _live_rows(self) -> int:
        """Rows required by the live content, capped without hiding its beginning."""
        return min(self._live_row_limit(), len(self._live_text.splitlines()))

    def _live_row_limit(self) -> int:
        """Fit the live tail around every other row the frame must keep visible."""
        if self._app is None:
            return ACTIVITY_ROWS
        try:
            size = self._app.output.get_size()
        except (AttributeError, ValueError):
            return ACTIVITY_ROWS
        width = max(1, size.columns)
        composer_rows = sum(
            max(
                1,
                (sum(get_cwidth(char) for char in line) + width - 1) // width,
            )
            for line in self._buffer.document.lines
        )
        composer_rows = min(COMPOSER_MAX_LINES, composer_rows)
        action_rows = ACTION_ROWS if self._action is not None else 0
        palette_rows = PALETTE_ROWS + 1 if self._palette_focused() else 0
        queue_rows = min(QUEUE_STACK_LINES, self._queue_rows())
        reserved = (
            STATUS_LINES
            + 2  # The composer's opening and closing rules.
            + TRANSCRIPT_GAP_LINES
            + composer_rows
            + action_rows
            + palette_rows
            + queue_rows
        )
        return max(0, size.rows - reserved)

    def _queue_rows(self) -> int:
        """Rows the queued-message surface currently needs, including its hint."""
        rows = self._queue_view.rows(self._pending)
        hint = self._queue_view.hint(running=self._running)
        return len(rows) + int(bool(hint))

    def _submit_typed(self, *, delivery: Delivery) -> None:
        """Submit the composer's text, restoring pasted payloads first.

        Clearing the composer does not always notify `_on_text_changed`, so the
        command list is closed explicitly here. Leaving it open put the palette and
        the action it just opened in the same place above the composer, so a reader
        saw two surfaces stacked and could not tell which one the arrows belonged to.
        """
        text = self._buffer.text
        self._buffer.reset()
        self._palette.close()
        expanded = self._pastes.expand(text)
        self._pastes.clear()
        if not expanded.strip():
            self.repaint()
            return
        selection = parse_selection_tokens(expanded)
        if self._running and (selection.skills or selection.mcp_servers):
            delivery = "queue"
            self.show_action(
                Action(message="Extension selection queued for the next turn.")
            )
        if delivery == "queue" and self._running:
            # A queued message waits for the turn, so it lives in the queue the
            # stack above the composer renders from.
            self._pending.push(PendingMessage(text=expanded, delivery="queue"))
        else:
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
            try:
                await self._app.run_async()  # type: ignore[union-attr]
            except EOFError:
                self._dismiss_action()
                raise

    async def aclose(self) -> None:
        """Stop the input line and release anything waiting on it."""
        self._dismiss_action()
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
        # Keep the complete renderable. The layout bounds transient activity; slicing
        # here also cut off the beginning of a pending Markdown paragraph, making a
        # stable answer look as if its first lines had been lost.
        self._live_text = text
        self.repaint()

    def flush_live(self) -> bool:
        """Paint the newest coalesced live text at a settle or layout boundary."""
        if self._flush_live is not None:
            return self._flush_live()
        return False

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

    #: Columns used before the terminal has reported a size. Wide enough not to wrap
    #: a normal answer needlessly, and replaced on the first paint.
    DEFAULT_WIDTH = 80

    def __init__(self, view: TranscriptView) -> None:
        self._view = view
        self._renderable: object = None
        self._pending: object = None
        self._view._flush_live = self.flush

    def start(self, renderable: object) -> None:
        self._renderable = renderable
        self._paint(force=True)

    def update(self, renderable: object) -> None:
        self._renderable = renderable
        self._paint()

    def stop(self) -> None:
        # Settle the newest coalesced renderable before taking the surface down.
        if self._pending is not None:
            self._paint(force=True)
        self._renderable = None
        self._view.show_live("")

    def flush(self) -> bool:
        """Paint a pending frame on a status/resize boundary."""
        if self._pending is not None:
            self._paint(force=True)
            return True
        return False

    def _paint(self, *, force: bool = False) -> None:
        """Render at the frame's current width, so a resize re-wraps the surface.

        The width is read on every paint rather than captured once: a region that
        remembered the width it started with would keep wrapping a 60-column terminal
        at 80 and overflow the frame after the operator resized.
        """
        now = self._view._clock()
        if not force and not self._view.pacer.admit(now):
            self._pending = self._renderable
            return
        if force:
            self._view.pacer.reset()
            self._view.pacer.admit(now)
        started = time.perf_counter()
        rendered = _render_to_text(
            self._renderable,
            width=self._view.available_width() or self.DEFAULT_WIDTH,
        )
        self._view.show_live(rendered)
        self._view.pacer.record_paint(duration=time.perf_counter() - started)
        self._pending = None


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
