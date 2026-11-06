import { useEffect, useRef, useState } from "react";

type Task = { sequence: number; mode: "conversation" | "development"; task_id: string; status: string; worktree: string | null; session_id: string | null; result: string | null; diagnostic: string | null; metrics: Record<string, unknown> };
type RecoveryReport = { expected_sequence: number; revision: unknown; action: string; fences: unknown[]; checkpoint_outcome: string | null };
type TaskEvent = { sequence: number; kind: string; data: Record<string, unknown> };

class RuntimeRequestError extends Error { constructor(public status: number) { super(`Request failed (${status})`); } }

async function api(path: string, body?: unknown) {
  const response = await fetch(`/api/runtime/${path}`, body === undefined ? undefined : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) throw new RuntimeRequestError(response.status);
  return response.json();
}

export function RuntimePanel({ onAvailable }: { onAvailable: (available: boolean) => void }) {
  const [available, setAvailable] = useState(false);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [active, setActive] = useState("");
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [history, setHistory] = useState<unknown[]>([]);
  const [preview, setPreview] = useState("");
  const [recovery, setRecovery] = useState<RecoveryReport | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [note, setNote] = useState("");
  const [mode, setMode] = useState<"conversation" | "development">("conversation");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<{ prompt: string; id: string; target: string; sequence: number } | null>(null);
  const selected = tasks.find(task => task.task_id === active);
  useEffect(() => {
    let live = true;
    const refresh = async () => {
      try {
        const data: Task[] = await api("tasks");
        if (live) { setTasks(data); setAvailable(true); onAvailable(true); }
      } catch { /* A normal read-only Web host does not expose Runtime routes. */ }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => { live = false; clearInterval(timer); };
  }, [onAvailable]);
  useEffect(() => {
    setEvents([]); setHistory([]); setPreview(""); setRecovery(null); setReviewed(false); setNote("");
    if (!active) return;
    let live = true;
    let cursor = 0;
    let socket: WebSocket | undefined;
    let retry: ReturnType<typeof setTimeout>;
    const connect = () => {
      socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/runtime/tasks/${active}/stream?after=${cursor}`);
      socket.onmessage = message => {
        if (!live) return;
        const data = JSON.parse(message.data);
        if (data.type === "event" && data.event.sequence > cursor) {
          cursor = data.event.sequence;
          setEvents(old => [...old, data.event].slice(-200));
          if (["running", "interrupted", "failed", "paused", "completed", "model_stream_start"].includes(data.event.kind) || (data.event.kind === "model_stream_end" && data.event.data.status !== "completed")) setPreview("");
        } else if (data.type === "preview") {
          setPreview(old => (old + data.text).slice(-64000));
        } else if (data.type === "settled") setPreview("");
      };
      socket.onclose = () => { if (live) { setPreview(""); retry = setTimeout(connect, 2000); } };
    };
    connect();
    return () => { live = false; clearTimeout(retry); socket?.close(); };
  }, [active]);
  async function submit() {
    if (!prompt.trim() || busy) return;
    if (!pending.current || pending.current.prompt !== prompt || pending.current.target !== active) pending.current = { prompt, id: crypto.randomUUID(), target: active, sequence: selected?.sequence ?? 0 };
    setBusy(true); setError("");
    try {
      const task: Task = selected ? await api(`tasks/${active}/messages`, { prompt, source: "web", request_id: pending.current.id, expected_sequence: pending.current.sequence }) : await api("tasks", { prompt, source: "web", request_id: pending.current.id, mode });
      setTasks(old => [...old.filter(item => item.task_id !== task.task_id), task]);
      setActive(task.task_id); setPrompt(""); pending.current = null;
    } catch (reason) { if (reason instanceof RuntimeRequestError && reason.status >= 400 && reason.status < 500) pending.current = null; setError(String(reason)); }
    finally { setBusy(false); }
  }
  async function control(action: string) {
    setBusy(true); setError("");
    try {
      const task: Task = await api(`tasks/${active}/${action}`, {});
      setTasks(old => old.map(item => item.task_id === task.task_id ? task : item));
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  async function reviewRecovery() {
    setBusy(true); setError("");
    try { setRecovery(await api(`tasks/${active}/recovery`)); setReviewed(false); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  async function reconcile() {
    if (!recovery || !reviewed || !note.trim()) return;
    setBusy(true); setError("");
    try {
      const task: Task = await api(`tasks/${active}/reconcile`, { expected_sequence: recovery.expected_sequence, revision: recovery.revision, action: recovery.action, reviewed_effects: true, note });
      setTasks(old => old.map(item => item.task_id === task.task_id ? task : item));
      setRecovery(null); setReviewed(false); setNote("");
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  if (!available) return null;
  return <section className="runtime-panel" aria-label="Shared Runtime tasks">
    <h2>Runtime tasks</h2>
    <button onClick={() => { setActive(""); setHistory([]); }} disabled={busy}>New conversation</button>
    {!selected && <label>Mode <select value={mode} onChange={event => setMode(event.target.value as "conversation" | "development")}><option value="conversation">Chat · development when needed</option><option value="development">Explicit development task</option></select></label>}
    <label htmlFor="runtime-prompt">{selected ? `Message ${selected.task_id}` : "New conversation or task"}</label>
    <textarea id="runtime-prompt" value={prompt} onChange={event => setPrompt(event.target.value)} maxLength={32000} rows={2} />
    <button disabled={busy || !prompt.trim() || Boolean(selected && !["completed", "paused"].includes(selected.status))} onClick={() => void submit()}>{selected ? "Send to this task" : "Start"}</button>
    <label>Execution record <select value={active} onChange={event => setActive(event.target.value)}><option value="">Select a task</option>{tasks.slice(-30).reverse().map(task => <option key={task.task_id} value={task.task_id}>{task.task_id} · {task.status}</option>)}</select></label>
    {selected && <div><p><strong>{selected.task_id}</strong> · {selected.status}</p><p>Worktree: {selected.worktree ?? "No development workspace created"}</p><pre>{selected.result ?? selected.diagnostic ?? "Running"}</pre><button disabled={busy || selected.status !== "paused"} onClick={() => void control("continue")}>Continue safely</button><button disabled={busy || !["queued", "running"].includes(selected.status)} onClick={() => void control("cancel")}>Cancel</button><details><summary>Budget and usage</summary><pre>{JSON.stringify(selected.metrics, null, 2)}</pre></details>{preview && <pre aria-live="polite">{preview}</pre>}{selected.status === "recovery_required" && <div><button disabled={busy} onClick={() => void reviewRecovery()}>Inspect recovery</button>{recovery && <><pre>{JSON.stringify(recovery, null, 2)}</pre><label><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} />I inspected the worktree and surviving effects</label><label>Surviving effects and next action<textarea value={note} onChange={event => setNote(event.target.value)} maxLength={1024} /></label><button disabled={busy || !reviewed || !note.trim()} onClick={() => void reconcile()}>Acknowledge without replay</button></>}</div>}<details><summary>Execution history</summary><ol>{events.map(event => <li key={event.sequence}>#{event.sequence} {event.kind} {JSON.stringify(event.data)}</li>)}</ol></details><details onToggle={event => { if (event.currentTarget.open) void api(`tasks/${active}/history`).then(setHistory).catch(reason => setError(String(reason))); }}><summary>Committed conversation · {selected.session_id}</summary><pre>{JSON.stringify(history, null, 2)}</pre></details></div>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
