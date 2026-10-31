# Personal Agent Memory

Personal memory belongs to one personal Agent, not to a project repository. It
is stored outside every Agent-writable Workspace under
`$XDG_STATE_HOME/cairn/agent` (default `~/.local/state/cairn/agent`, 0700):

```text
AGENT.md                         personal operating preferences and learned rules
MEMORY.md                        curated long-term memory
memory/YYYY/MM/YYYY_MM_DD.md     daily journal entries
memory/AGENT_CHANGES.md          AGENT.md proposal/approval audit
memory/AGENT_HISTORY/            pre-change snapshots for rollback
pending_agent_update.json        one pending AGENT.md proposal
```

## Commands

```text
/memory show                 show paths and current sizes
/memory agent                show AGENT.md
/memory note <text>          append an explicit preference to today's journal
/memory distill [days]       extract valuable text/result candidates into MEMORY.md
/memory propose <text>       create a pending AGENT.md update from your text
/memory propose-auto [days]   let the agent derive a pending update from daily/MEMORY facts (with date sources)
/memory pending              show the complete pending candidate, reason and source dates
/memory approve              apply the pending update and write an audit record
/memory reject               discard the pending update
/memory rollback             restore the previous AGENT.md snapshot
/memory files                show storage paths
```

## Context and safety

At the start of a task the personal memory is read as bounded, JSON-escaped
lower-trust **user data**, never merged into the system prompt or treated as
authority. Project `AGENTS.md`, system rules, permissions and the live user
request always take precedence. A memory text cannot grant tools, network or
filesystem authority.

`AGENT.md` is never changed silently: the default workflow is propose -> human
approve -> atomic write with a snapshot and audit trail. Rollback restores the
last snapshot. The first version deliberately does not use embeddings,
Dreaming or automatic model-driven rewriting.
