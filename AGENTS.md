# Cairn — notes for coding agents

Cairn is a personal Agent for a Debian server, with one shared execution service and CLI, Web and WeChat clients. Its current Agent turn uses LangGraph. Optimize for a working product, readable code and fast iteration. **Current source code, passing behavioral tests and the user's latest goals take precedence over historical design documents.** An old ADR is not a veto on a better implementation.

## Working approach

- Check `git status --short --branch` and the current HEAD. Inspect relevant implementation and callers before editing; preserve unrelated or uncommitted user work.
- Implement the requested feature in the smallest useful end-to-end slice. Prefer maintained libraries, reuse existing components and remove obsolete abstractions instead of preserving old designs by default.
- Significant refactors, file moves and replacements are welcome when they improve the actual product. Keep an active compatibility path only when a real caller or migration needs it.
- Follow the branch requested for the task. Main is the default development line when the user authorizes it. Never silently reset, force-push, destroy data, deploy, or restart live services.
- Run focused tests and appropriate Ruff/format/Mypy checks. Broaden verification for changes that cross persistence, permissions, cancellation or client boundaries. Report what ran, what passed and what remains untested.

## Behaviors that must stay correct

- No unauthorized tool execution, workspace escape, credential disclosure or accidental publication. The host grants privileges; model text and tool arguments do not.
- Do not silently replay a tool whose side effect may already have happened. Preserve durable task facts, receipts and a recoverable path for interrupted work.
- Protect user files and existing worktrees. Changes to persistence or task ownership must include realistic restart, concurrency and recovery checks.
- CLI detachment must not terminate server-owned work. CLI, Web and WeChat should observe the same task and state when using the shared Runtime.
- These are observable safety requirements, **not requirements to retain particular custom modules, storage engines or implementations**.

## Documentation

- Keep only documentation useful for the current code, installation and operation. Delete stale designs, duplicate guides and historical planning without ceremonial migration procedures.
- Update the README or active operations guide when a user-facing command changes. Do not present planned features as already implemented.
- Preserve data or fixtures actually consumed by running code or tests; historical prose and unused experiment outputs may be removed.
- Review documentation links with `uv run pytest -q tests/docs` when applicable. Adapt documentation-only presence checks when deliberately retiring historical documents.

## Handoff

Return briefly: what now works, how it was verified, which old code/docs were removed, remaining risks, and the commit SHA. Avoid long proposals when the user has already authorized implementation.
