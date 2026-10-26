# Interactive terminal

What the operator sees and presses while a session runs. This describes the
current design; the guarantees each surface rests on live in the guides linked at
the end.

## The bottom dock

One viewport owns the bottom of the terminal. Finished output is printed into the
terminal's real scrollback above it, so native search, selection and copy keep
working; only the dock repaints. From top to bottom:

```text
* Thinking  00:03                       <- activity: what the turn is doing now
  Reading the current parser and its callers...
  Checking how tokens reach the tree...    Ctrl+O: details
main  ·  provider bailian               <- action: a decision or a correction
  > flash    current
    plus
  Up/Down move   Enter select   Esc cancel
>                                        <- queued messages, if any
--------------------------------------------------------------
> |                                      <- the composer
--------------------------------------------------------------
◇ deepseek-v4.1-flash-1017  ·  via bailian
↑ 1.2k  ↓ 84  ·  28 tok/s  ·  ctx [##--------] 18%
```

Nothing else is permanent. The activity slot and the action slot collapse when
empty. One intentional blank row separates the newest output from the composer.
That keeps the composer and the two status rows in the same place while the
transient surfaces open and close.

## Information lifetimes

Every surface exists because of how long its content is worth keeping:

| Region | Lives | Holds |
| --- | --- | --- |
| Transcript | Permanent | The prompt, the final answer, one reasoning receipt |
| Activity | While the turn runs | Thinking preview, running tool, interrupt message |
| Action | Until answered or replaced | Corrections, refusals, `/model` and `/provider` choosers |
| Composer | Always | The line being typed |
| Status | Session | Two rows: model and provider, then usage |

## The transcript

A turn is two blocks under a two-column gutter:

```text
> how does the parser work?

  Thinking  3.2s
* It splits tokens, then builds a tree.
```

The left edge is the interface. A glyph opens each turn — `>` for the operator,
`*` for the reply — and continuation lines align under it, so the column of
glyphs reads as a rhythm and the transcript stays scannable in peripheral vision.

- **No role labels.** A word at the left edge turns the rhythm into a table of
  contents and costs a line per turn.
- **The glyph leads the first line only.** A marker on every line is copied along
  with any code the reader selects.
- **Both glyphs are ASCII.** A glyph whose display width depends on the terminal's
  East Asian setting would shift every continuation line by a column.

Tool calls do not enter the transcript. A call is transient — it is happening, then
it has happened — and the trace keeps the full record. A *failure* is the exception:
a denied call or a failed command is the one tool event a reader must act on, so it
still prints, with its exit code and a bounded diagnostic.

## The activity slot

Thinking, tool work and an interrupt share one place, because a reader asking "what
is happening" should not have to look in three.

```text
* Thinking  00:03
  Reading the current parser and its callers...
  Checking how tokens reach the tree...    Ctrl+O: details
```

- **Two rendered rows.** The window shows the last two rows *after wrapping at the
  current width*, not two logical lines and not two provider deltas. A delta can end
  mid-word, so slicing by delta would show fragments of one sentence.
- **A frame and a clock that actually run.** `.` `*` `+` `*` at most four times a
  second, advanced by the repaint the display already does. Without that the clock
  froze at `00:00` whenever the model paused, which looks like a hang.
- **`Ctrl+O` toggles.** It widens the window and puts it back, and the hint changes
  from `details` to `less`. It is offered only while the preview runs; after the turn
  there is nothing to reveal, so the key disappears with the surface.
- **The first answer token freezes it.** Thinking leaves immediately, and one dim
  receipt prints above the first visible answer. Its duration stops at that token.
  Later model steps and retries cannot reopen the preview below or inside the reply.

```text
* read_file  src/parser.py
  Checking the parser implementation
```

A running tool names itself and its subject. Several in one batch report how many
share the work, and the surface keeps saying so until the batch settles — reporting
"thinking" while three calls still execute would describe work that has not started.

```text
* Interrupting...
```

`Ctrl+C` during a turn shows this while cleanup completes.

## The action slot

Corrections, refusals and choosers appear here, above the composer, and are gone
when answered. They are not transcript text: a note about a typo, or a list of
commands, is a means to an action rather than a record of one.

An unknown command is corrected in place and cleared by the next keystroke:

```text
/session is not an in-session command. Type / to browse, or /help for the list.
```

### Choosing a model

`/model` opens the groups the provider actually configures, and expands only the
one under the cursor — its primary and any fallback IDs, with the most recent
failure if there was one:

```text
model  deepseek-v4.1
  > flash    current
      primary   openai/deepseek-v4.1-flash-1017
      fallback  none
    plus
  Up/Down move   Enter select   Esc cancel
```

`/model use <group>` and confirming a row run the same command, so the menu and the
typed form share one path through the runtime and its build-before-commit rules.
`/model list` still prints the whole catalogue, for a reader who wants it in the
scrollback.

### Switching a provider

Switching sends the conversation somewhere else, so the surface states the whole
decision before anything happens:

```text
Send this session's conversation to bailian:
  https://<configured-endpoint>/compatible-mode/v1
Credential variable  CAIRN_LLM_API_KEY
Models              openai/deepseek-v4.1-flash-1017
The full conversation history moves with it, and session tool permissions are reset.
  > bailian    current
    local
  Up/Down move   Enter switch   Esc cancel
```

The endpoint is shown in full, because a lookalike host is exactly what a shortened
one would hide. The credential appears as a *variable name* and is not resolved
until the operator confirms. `Esc` and `Ctrl+D` decline, `Enter` is never the
default, and a declined or failed switch keeps the current provider.

When decision facts are longer than the action viewport, `PageUp` and `PageDown`
page through the wrapped facts. The selected choice row stays visible on every page,
including the safe cancellation row; `Up` and `Down` continue to move the selection.

## The command palette

Typing `/` opens the command list above the composer, filtered as the text grows:

```text
> /mo

+ Commands ------------------------------------------+
| > /model     Choose the active model group          |
+-----------------------------------------------------+
  Up/Down move · Enter open · Tab complete · Esc close
```

It is a temporary surface, not transcript text: a list of commands is a means to an
action, so closing it leaves nothing behind. It sits *above* the composer rather than
between the composer's rules and the text being edited, which would look like the
input had moved.

| Key | Effect |
| --- | --- |
| `Up` / `Down` | Move the selection |
| `Enter` | Run the chosen command: open its level, or submit it when it is complete |
| `Right` | Open the chosen command's next level, without running anything |
| `Tab` | Put the chosen command in the composer, without running it |
| `Esc` | Close the palette, without touching a running turn |

`Enter` runs what is chosen, the same as it does in the composer, so ending the
session is typing `/exit` once and pressing `Enter` once. A command that opens a
level is not run by the first `Enter`: that press opens the level, and the level
chooses the rest of the command line. `/exit` is the only command that ends the
session; there is no alias.

Filtering is by prefix, not substring: `/mo` means "the command starting with mo",
and answering with `/compact` because it contains an `m` would be a guess. The
palette follows the text rather than a key, so it closes the moment the text stops
looking like a command — including when the slash is deleted.

## Steering a running turn

While a turn runs, a typed message is not a new turn. It joins the one in flight at
its next step boundary, so the work already done is kept.

| Key | While a turn runs | While idle |
| --- | --- | --- |
| `Enter` | Guide the running turn (steer) | Start work |
| `Tab` | Queue the message for after the turn | Submit |
| `Up` / `Down` | Move through the queued messages | Move through input history |
| `Enter` with the composer empty | Promote the selected queued message to a guide | — |
| `Delete` with the composer empty | Discard the selected queued message | — |
| `Ctrl+O` | Toggle more of the thinking preview | — |
| `Ctrl+C` | Unwind one layer, then interrupt | — |
| `Ctrl+D` | End input | End input |

`Ctrl+C` unwinds one layer at a time, because the operator cannot see which layer
has focus: an open chooser or correction is declined first, then the palette closes,
then the composer clears, and only then is a running turn stopped. Pressing it again
while a cancellation is in flight does not re-request the cancellation and does not
print a second receipt.

Queued messages are shown as a stack above the composer with the selected one
marked, because a message that is accepted and then invisible is indistinguishable
from one that was dropped. The two corrections an operator needs are opposite: a
message that became urgent is promoted, one that became irrelevant is discarded.

A message arriving as the model finishes is still delivered, and a message arriving
during a tool batch lands before the next model call rather than after the turn.

## The status rows

Exactly two rows, and nothing beneath them:

```text
◇ deepseek-v4.1-flash-1017  ·  via bailian
↑ 1.2k  ↓ 84  ·  28 tok/s  ·  ctx [##--------] 18%
```

The two rows group identity and usage separately. The model is accented, while the
provider, labels and separators are secondary. Usage totals use compact `k` and `M`
units; the context bar is decorative and gives way to the percentage on narrow
terminals. Unicode marks have ordinary ASCII fallbacks, and the rows remain readable
without color.

They answer different questions and age differently: what the session talks to
changes when the operator switches, while the numbers change every second. A rate
freezes when its turn ends — left live it would decay with the idle clock, reading as
a model that slows down after finishing — and an unmeasured session says `ready`
rather than showing zeros that look measured.

On a narrow terminal the order of sacrifice is fixed: shorten the model id, then drop
the context figure, then drop usage. The provider and model never both disappear,
because a reader who cannot tell what they are talking to has lost the most important
fact on the line. The full ids remain reachable through `/model`.

## Startup

Two questions are asked, each for a reason the operator has to weigh: which provider
receives the conversation, and whether this directory is trusted. Both are asked
before the effect they authorise. The provider question names the credential it will
read and the models it covers; the directory question names the directory.

Everything else that used to be printed — endpoint, credential variable, model
list — is either part of the question or already known to the operator.

## Related

- [Local command execution](local-execution.md) — what the shell does and does not
  guarantee.
- [Model streaming](model-streaming.md) — how text arrives, settles and is redrawn.
- [Session discovery](session-discovery.md) — listing and reopening stored sessions.
- [Durable CLI](durable-cli.md) — approval, inspection and reconciliation.
