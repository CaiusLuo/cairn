# Interactive terminal

What the operator sees and presses while a session runs. This describes the
current design; the guarantees each surface rests on live in the guides linked at
the end.

## The transcript

A turn is two blocks under a two-column gutter:

```text
> how does the parser work?

* It splits tokens, then builds a tree.

> and the cache?

* Keyed by file digest.
```

The left edge is the interface. A glyph opens each turn — `>` for the operator,
`*` for the reply — and continuation lines align under it, so the column of
glyphs reads as a rhythm and the transcript stays scannable in peripheral vision.

Three rules follow from that and are worth stating because each is easy to break:

- **No role labels.** A word at the left edge turns the rhythm into a table of
  contents and costs a line per turn; the shipped agents do not print one, and a
  rendered role marker is treated as a defect rather than a feature.
- **The glyph leads the first line only.** A marker on every line is copied along
  with any code the reader selects.
- **Both glyphs are ASCII.** A glyph whose display width depends on the terminal's
  East Asian setting would shift every continuation line by a column.

Progress is not part of the transcript. The step counter, token rate and context
usage live on the status area under the input, because a turn with a hundred steps
would otherwise leave a hundred lines that say nothing once it ends.

## Information lifetimes

Every surface exists because of how long its content is worth keeping:

| Region | Lives | Holds |
| --- | --- | --- |
| Transcript | Permanent | The prompt and the final reply |
| Preview | While a turn runs | The newest two rows of reasoning, a frame and a clock |
| Status | Session | What the session talks to, and what it has spent |

Reasoning is temporary by design. It shows its newest two rows while the model
thinks — the opening rows aged badly, describing a thought the model had already
moved past — and when the turn settles only a receipt remains:

```text
* Hi! I'm Cairn.

Thinking  3.2s
```

The text itself belongs to the trace. Reprinting it into the scrollback cost a
screenful per turn, next to the answer that already summarised it.

`Ctrl+O` widens the preview by a step, so a long thought can be read while it
arrives. It is offered only while the preview runs: the reasoning is not kept after
the turn, so a settled turn has nothing to reveal and the key genuinely does
nothing. The hint appears and disappears with the preview rather than persisting in
the scrollback, which is what made it a false promise before.

Expansion widens the same temporary viewport upward through the text already
received. It never reaches into the transcript, because the transcript never held
the reasoning in the first place.

The status area is two lines because they answer different questions and age
differently: the model and provider change when the operator switches, while the
numbers change every second. A rate freezes when its turn ends — left live it would
decay with the idle clock, reading as a model that slows down after finishing — and
an unmeasured session says `ready` rather than showing zeros that look measured.

## The composer

The input is bracketed by two horizontal rules with no side segments: a rule spans
the current width for free, while a full box must be re-measured on every resize.

It grows to four lines and then scrolls internally, so a long message never pushes
the transcript around. A large paste is replaced by a marker such as
`[pasted 12 lines]`, and the payload is restored on submit — the composer stays
readable while the message still carries what was pasted.

## The command palette

Typing `/` opens a list of commands above the composer, filtered as the text grows:

```text
> /mo

+ Commands ------------------------------------------+
| > /model     Choose the active model group          |
+-----------------------------------------------------+
  Up/Down move · Tab or Enter complete · Esc close
```

It is a temporary surface, not transcript text: a list of commands is a means to an
action, so closing it leaves nothing behind.

The palette is a stack of levels, not a flat list. A command that takes a
sub-command opens one rather than running something that prints into the
conversation:

```text
Commands > /model
  > use        Select a model group for this session
    list       Show groups with their primary and fallback IDs
```

| Key | Effect |
| --- | --- |
| `Up` / `Down` | Move the selection within the level |
| `Right` / `Enter` | Descend into the chosen command, or confirm it |
| `Left` | Go back one level; at the root there is nowhere to go |
| `Tab` | Put the chosen command in the composer |
| `Esc` | Close the palette, without touching a running turn |

The keys mean the same thing at every level, which is what makes the surface
learnable. A level remembers its own selection, so coming back puts the cursor where
it was. The breadcrumb names where you are, because the same `use` row means a model
group here and a provider one level away.

Filtering is by prefix, not substring: `/mo` means "the command starting with mo",
and answering with `/compact` because it contains an `m` would be a guess. The
palette follows the text rather than a key, so it closes the moment the text stops
looking like a command — including when the slash is deleted.

## Steering a running turn

While a turn runs, a typed message is not a new turn. It joins the one in flight at
its next step boundary, so the work already done is kept: the turn keeps its
identity, its tool batch and its cancellation scope.

| Key | While a turn runs | While idle |
| --- | --- | --- |
| `Enter` | Guide the running turn (steer) | Start work |
| `Tab` | Queue the message for after the turn | — |
| `Up` / `Down` | Move through the queued messages | Move through input history |
| `Enter` with the composer empty | Promote the selected queued message to a guide | — |
| `Delete` with the composer empty | Discard the selected queued message | — |
| `Ctrl+O` | Show more of the collapsed reasoning | — |
| `Ctrl+C` | Interrupt | — |
| `Ctrl+D` | End input | End input |

Queued messages are shown as a stack above the composer with the selected one
marked, because a message that is accepted and then invisible is indistinguishable
from one that was dropped. The two corrections an operator needs are opposite: a
message that became urgent is promoted, one that became irrelevant is discarded.

A message arriving as the model finishes is still delivered — the turn does not
end while a guide is waiting — and a message arriving during a tool batch lands
before the next model call rather than after the turn.

## Startup

Two questions are asked, each for a reason the operator has to weigh: which
provider receives the conversation, and whether this directory is trusted. Both are
asked before the effect they authorise. The provider question names the credential
it will read and the models it covers; the directory question names the directory.

Everything else that used to be printed — endpoint, credential variable, model
list — is either part of the question or already known to the operator.

## Related

- [Local command execution](local-execution.md) — what the shell does and does not
  guarantee.
- [Model streaming](model-streaming.md) — how text arrives, settles and is redrawn.
- [Session discovery](session-discovery.md) — listing and reopening stored sessions.
- [Durable CLI](durable-cli.md) — approval, inspection and reconciliation.
