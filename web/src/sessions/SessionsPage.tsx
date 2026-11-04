import { useEffect, useMemo, useRef, useState } from "react";
import { Sidebar } from "../components/Sidebar";
import { SyncBadge } from "../components/SyncBadge";
import { useLiveSnapshot } from "../useLiveSnapshot";
import {
  fetchSession,
  shortId,
  sourceClassName,
  sourceLabel,
  stateLabels,
  type SessionDetail,
  type SessionSummary
} from "../lib";

type Scope = "all" | "web" | "cli" | "wechat" | "schedule";

const SCOPE_LABELS: { key: Scope; label: string }[] = [
  { key: "all", label: "All" },
  { key: "web", label: "Web" },
  { key: "cli", label: "CLI" },
  { key: "wechat", label: "WeChat" },
  { key: "schedule", label: "Schedule" }
];

function stateDot(state: SessionSummary["state"]): string {
  if (state === "executing") return "dot dot--live";
  if (state === "recovery_required") return "dot dot--waiting";
  return "dot dot--idle";
}

function sessionTitle(session: SessionSummary): string {
  return session.title?.trim() || `Untitled · ${session.short_id}`;
}

function typeIcon(source: string | null) {
  if (source === "cli") {
    return <path d="m3.8 4.5 3.3 3.5-3.3 3.5M8.8 11.5h3.4" />;
  }
  if (source === "wechat") {
    return <path d="M3.2 3.5h9.6a1 1 0 0 1 1 1v6.2a1 1 0 0 1-1 1H7l-3.2 2v-2H3.2a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1Z" />;
  }
  if (source === "schedule") {
    return <><circle cx="8" cy="8" r="5.2" /><path d="M8 4.8V8l2.2 1.4" /></>;
  }
  return <><circle cx="8" cy="8" r="5.4" /><path d="M2.8 8h10.4M8 2.6c1.4 1.5 2.1 3.4 2.1 5.4S9.4 12 8 13.4C6.6 12 5.9 10 5.9 8S6.6 4.1 8 2.6Z" /></>;
}

export function SessionsPage() {
  const live = useLiveSnapshot();
  const snapshot = live.snapshot;
  const error = live.error;
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [detailError, setDetailError] = useState(false);
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<Scope>("all");
  const transcriptRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setSelectedId((current) => {
      if (current && snapshot?.sessions.some((session) => session.session_id === current)) {
        return current;
      }
      const requested = new URLSearchParams(location.search).get("session");
      return requested && snapshot?.sessions.some((session) => session.session_id === requested)
        ? requested
        : snapshot?.sessions[0]?.session_id ?? null;
    });
  }, [snapshot?.sessions]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let live = true;
    setDetailError(false);
    setDetail(null);
    fetchSession(selectedId)
      .then((next) => {
        if (live) setDetail(next);
      })
      .catch(() => {
        if (live) setDetailError(true);
      });
    return () => {
      live = false;
    };
  }, [selectedId, snapshot?.sequence]);

  useEffect(() => {
    const end = transcriptRef.current;
    if (end) end.scrollTop = end.scrollHeight;
  }, [detail]);

  const sessions = snapshot?.sessions ?? [];
  const filtered = useMemo(() => {
    const text = query.trim().toLowerCase();
    return sessions.filter((session) => {
      const inScope = scope === "all" || session.source === scope;
      const inSearch =
        !text ||
        sessionTitle(session).toLowerCase().includes(text) ||
        session.short_id.toLowerCase().includes(text);
      return inScope && inSearch;
    });
  }, [sessions, query, scope]);

  const selected = sessions.find((session) => session.session_id === selectedId) ?? null;
  const selectedSourceName = sourceLabel(selected?.source ?? null);

  return (
    <div className="shell">
      <Sidebar active="sessions" sessionsCount={sessions.length} tasksCount={snapshot?.tasks.length ?? 0}>
        <button className="sidebar__action" disabled title="Read-only W1">
          <span aria-hidden="true">+</span> New session
        </button>
        <div className="search">
          <svg className="i i--sm" viewBox="0 0 16 16"><circle cx="7.2" cy="7.2" r="4.1"/><path d="m10.3 10.3 2.7 2.7"/></svg>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search sessions" aria-label="Search sessions" />
        </div>
        <div className="scope" role="tablist" aria-label="Session source">
          {SCOPE_LABELS.map((item) => (
            <button key={item.key} className={`scope__btn${scope === item.key ? " is-active" : ""}`} onClick={() => setScope(item.key)} role="tab">
              {item.label}
            </button>
          ))}
        </div>
        <div className="sidebar__section">
          <div className="sidebar__label">{snapshot?.source_status.sessions === "ok" ? "Stored sessions" : "Sessions unavailable"}</div>
          {filtered.length === 0 ? (
            <div className="empty-sidebar">{query || scope !== "all" ? "No matching sessions" : "No stored sessions"}</div>
          ) : (
            filtered.map((session) => (
              <button
                key={session.session_id}
                className={`session-row${session.session_id === selectedId ? " is-active" : ""}`}
                onClick={() => setSelectedId(session.session_id)}
              >
                <span className="session-row__icon"><svg className="i" viewBox="0 0 16 16">{typeIcon(session.source)}</svg></span>
                <span className="session-row__body">
                  <span className="session-row__title">{sessionTitle(session)}</span>
                  <span className="session-row__meta">
                    <span className={sourceClassName(session.source)}>{sourceLabel(session.source)}</span>
                    {session.task_ids[0] ? <span className="tag tag--task" title={session.task_ids.join(", ")}>{shortId(session.task_ids[0], 10)}{session.task_ids.length > 1 ? ` +${session.task_ids.length - 1}` : ""}</span> : null}
                  </span>
                </span>
                <span className={stateDot(session.state)} />
              </button>
            ))
          )}
        </div>
      </Sidebar>

      <main className="session-main">
        <header className="session-head">
          <div className="session-head__identity">
            <h1>{selected ? sessionTitle(selected) : "Sessions"}</h1>
            {selected ? <span className={sourceClassName(selected.source)}>{selectedSourceName}</span> : null}
            {selected?.task_ids.map((taskId) => (
              <a className="task-link" key={taskId} href={`/tasks.html?task=${encodeURIComponent(taskId)}`} title={taskId}>
                <svg className="i" viewBox="0 0 16 16"><path d="M3.2 4.5h1M6.2 4.5h6.6M3.2 8h1M6.2 8h6.6M3.2 11.5h1M6.2 11.5h4.2"/><path d="m2.7 4.4.5.5.8-.9M2.7 7.9l.5.5.8-.9"/></svg>
                {shortId(taskId, 10)}
              </a>
            ))}
            {selected ? <span className="state-tag">{stateLabels[selected.state]}</span> : null}
          </div>
          <div className="session-head__actions">
            <SyncBadge sync={live.sync} />
            <span className="read-only-badge">Read-only</span>
          </div>
        </header>

        <div className="conversation-body">
          <div className="transcript" ref={transcriptRef} tabIndex={0} aria-label="Transcript">
            <div className="transcript__inner">
              {error ? (
                <div className="empty-state">Snapshot unavailable. Check the startup roots and session store.</div>
              ) : !selected ? (
                <div className="empty-state">No sessions in the configured store.</div>
              ) : detailError ? (
                <div className="empty-state">Session history unavailable.</div>
              ) : !detail ? (
                <div className="empty-state">Loading committed history…</div>
              ) : detail.messages.length === 0 ? (
                <div className="empty-state">No committed messages in this Session.</div>
              ) : (
                detail.messages.map((message, index) => (
                  <article key={`${message.role}-${index}`} className={`turn turn--${message.role === "assistant" ? "assistant" : message.role === "user" ? "user" : "tool"}`}>
                    <div className="turn__marker"><span className="turn__glyph">{message.role === "user" ? "›" : message.role === "assistant" ? "*" : "·"}</span></div>
                    <div className="turn__content"><p>{message.content}</p></div>
                  </article>
                ))
              )}
              <div id="transcriptEnd" />
            </div>
          </div>
        </div>

        <div className="dock">
          <div className="composer composer--readonly">
            <div className="composer__field">
              <textarea rows={1} disabled placeholder="Read-only in W1" aria-label="Read-only composer" />
            </div>
            <button className="send" disabled aria-label="Send unavailable in W1">
              <svg className="i i--sm" viewBox="0 0 16 16"><path d="M8 12.5V4.2M4.5 7.7 8 4.2l3.5 3.5"/></svg>
            </button>
          </div>
          <div className="status">
            <div className="status__row">
              <span className="status__model">{selected ? `Session ${selected.short_id}` : "No session selected"}</span>
              {selected ? <><span>{stateLabels[selected.state]}</span></> : null}
            </div>
            <div className="status__row">
              {selected ? <><span>head {selected.head_seq}</span></> : null}
              <span>read-only</span>
              <span>{snapshot?.source_status.sessions === "ok" ? "snapshot" : snapshot?.source_status.sessions ?? "initialising"}</span>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
