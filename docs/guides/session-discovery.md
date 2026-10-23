# Session discovery, titles and resumed work

This guide states what exists today for finding and resuming a stored
conversation, and the intended order for the parts that do not exist yet. It
describes the current design; change history belongs in commits.

## Finding a conversation

`cairn session list` prints the stored Sessions, most recently updated first:

```text
ID       STATE               UPDATED               TITLE
14d813   ready               2026-10-23T10:00:00Z  (untitled; ...)
8a2563   ready               2026-10-23T10:01:00Z  (untitled; ...)

Reopen one with: cairn session reopen <ID>
```

`ID` is the first six characters of the Session identifier. Any command that
takes a Session also accepts that abbreviation:

```sh
cairn session inspect 14d813
cairn session reopen 14d813
```

Resolution refuses rather than guesses:

| Input | Outcome |
| --- | --- |
| A whole identifier, hyphenated or not | The Session, with no scan |
| A unique prefix | The Session |
| Two Sessions sharing the prefix | `ambiguous_session_id` |
| Non-hexadecimal or longer than an identifier | `invalid_session_id` |
| Hex characters, no matching Session | `session_not_found` |

The distinction matters because reopening the wrong conversation resumes work
against the wrong history, so an ambiguous abbreviation must name no Session at
all.

Listing reads the Session row only. It does not decode a checkpoint or replay an
attempt, so finding a conversation costs the same whether a Session has one turn
or a hundred. `cairn session inspect <ID>` is the call that answers whether a
given Session can actually be reopened.

Storage is bounded: `MAX_LISTED_SESSIONS` caps one listing, and `--limit` is
refused when it is not a positive integer.

## Session titles

A Session has no title yet. `cairn session list` prints an explicit placeholder
rather than inventing one, so a reader is never shown a guessed title that looks
authoritative.

The intended contract, in the order it will be built:

1. **Derive a provisional title from the first user turn.** Truncate at the first
   sentence or clause boundary, bounded in length, and never truncate mid
   grapheme. This needs no model call and no schema change to display.
2. **Generate the title once, after the first reply.** One small request, ordered
   with the turn, so the label describes what the conversation actually became
   rather than what its first question guessed. It is written once and does not
   drift with later turns.
3. **Let a human edit it.** `cairn session title <ID> "<text>"` overwrites the
   generated value. An edit is durable and is never silently regenerated: a later
   turn must not overwrite a title a person chose.

Two constraints belong to the design, not to the implementation order:

- **A title is not an authority.** It is display text for the operator. It must
  never select a Session, and it must never be read as instructions by the model.
- **A derived title must not leak credential material.** A first user message can
  quote a secret; a title is rendered in listings, so the derivation passes the
  same `is_credential_path`-style refusal as any other read, and the generation
  request carries no file content.

Storing a title needs a schema change. `sessions` currently carries
`(session_id, version, head_seq, state, active_turn_id, binding_json,
created_at_utc, updated_at_utc)`, and the schema is version 4 with exact
`sqlite_schema` comparison, so adding a column is a versioned migration with its
own validation path — not a drive-by `ALTER TABLE`.

## Resuming work: Tasks and goals

`DurableSession` already owns turn-level recovery: an unfinished Turn is either
reconciled (abandoned without replay) or reopened with fresh provider approval and
an explicit binding acknowledgement. That covers a conversation, not the work
inside it.

`TaskLedgerMixin` already persists Tasks with a revision and explicit recovery
phases. The remaining gap is the operator-facing path:

1. **Show work along with the conversation.** `cairn session list` should say
   whether a Session has an unfinished Task, so "resume" is not only about
   messages. The listing stays one row per Session.
2. **One resumption decision, not two.** Reopening must reconcile conversation
   state and Task state through a single acknowledgement, because two independent
   approvals for one interrupted run is a defect: a human could approve the Turn
   and refuse the Task, leaving work that appears abandoned but is still owned.
3. **Goals sit above Tasks.** A goal is the durable statement of what the work is
   for; a Task is one attempt at it. A resumed Task must be admitted against an
   unchanged goal, and a goal that changed while the Session was closed must
   require a new decision rather than silently continuing.

None of this changes `runtime.loop.run_turn`, which stays the only Agent turn. The
Task and goal layers describe and gate work; they must not acquire their own loop.

## Compaction while work is running

Tool output is already reduced between steps: settled tool results are shortened
to a placeholder, and the newest group is kept readable, so every call stays
paired with its result. A tool result is never dropped, only shortened, because a
result without its call is invalid for every provider.

That reduction is threshold-driven from the request budget, not per call. Cutting
after every tool call would spend requests on conversations that still fit, and
would discard the call structure the next step reasons from.

## Related

- [Session store](session-store.md) — binding, locking and fail-closed admission.
- [Durable CLI](durable-cli.md) — approval, inspection and reconciliation.
- [Durable Task Ledger](task-ledger.md) — Task phases and explicit recovery.
- [Local command execution](local-execution.md) — the boundary tools do not claim.
