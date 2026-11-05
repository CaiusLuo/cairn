import { useEffect, useRef, useState } from "react";

type Task = { task_id: string; status: string; worktree: string | null; session_id: string | null; result: string | null; diagnostic: string | null; metrics: Record<string, unknown> };
type TaskEvent = { sequence: number; kind: string; data: Record<string, unknown> };

async function api(path: string, body?: unknown) {
  const response = await fetch(`/api/runtime/${path}`, body === undefined ? undefined : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

export function RuntimePanel({ onAvailable }: { onAvailable: (available: boolean) => void }) {
  const [available, setAvailable] = useState(false);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [active, setActive] = useState("");
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [history, setHistory] = useState<unknown[]>([]);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<{ prompt: string; id: string } | null>(null);
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
    if (!active) return;
    let live = true;
    let cursor = 0;
    let fetching = false;
    setEvents([]); setHistory([]);
    const refresh = async () => {
      if (fetching) return;
      fetching = true;
      try {
        const data: { events: TaskEvent[]; cursor: number } = await api(`tasks/${active}/events?after=${cursor}`);
        if (live) { cursor = data.cursor; setEvents(old => [...old, ...data.events].slice(-200)); }
      } catch (reason) { if (live) setError(String(reason)); }
      finally { fetching = false; }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 1000);
    return () => { live = false; clearInterval(timer); };
  }, [active]);
  async function submit() {
    if (!prompt.trim() || busy) return;
    if (!pending.current || pending.current.prompt !== prompt) pending.current = { prompt, id: crypto.randomUUID() };
    setBusy(true); setError("");
    try {
      const task: Task = await api("tasks", { prompt, source: "web", request_id: pending.current.id });
      setTasks(old => [...old.filter(item => item.task_id !== task.task_id), task]);
      setActive(task.task_id); setPrompt(""); pending.current = null;
    } catch (reason) { setError(String(reason)); }
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
  if (!available) return null;
  return <section className="runtime-panel" aria-label="Shared Runtime tasks">
    <h2>Runtime tasks</h2>
    <label htmlFor="runtime-prompt">New development task</label>
    <textarea id="runtime-prompt" value={prompt} onChange={event => setPrompt(event.target.value)} maxLength={32000} rows={2} />
    <button disabled={busy || !prompt.trim()} onClick={() => void submit()}>Submit task</button>
    <label>Execution record <select value={active} onChange={event => setActive(event.target.value)}><option value="">Select a task</option>{tasks.slice(-30).reverse().map(task => <option key={task.task_id} value={task.task_id}>{task.task_id} · {task.status}</option>)}</select></label>
    {selected && <div><p><strong>{selected.task_id}</strong> · {selected.status}</p><p>Worktree: {selected.worktree ?? "Preparing"}</p><pre>{selected.result ?? selected.diagnostic ?? "Running"}</pre><button disabled={busy || selected.status !== "paused"} onClick={() => void control("continue")}>Continue safely</button><button disabled={busy || !["queued", "running"].includes(selected.status)} onClick={() => void control("cancel")}>Cancel</button><details><summary>Execution history</summary><ol>{events.map(event => <li key={event.sequence}>#{event.sequence} {event.kind} {JSON.stringify(event.data)}</li>)}</ol></details><details onToggle={event => { if (event.currentTarget.open) void api(`tasks/${active}/history`).then(setHistory).catch(reason => setError(String(reason))); }}><summary>Committed conversation · {selected.session_id}</summary><pre>{JSON.stringify(history, null, 2)}</pre></details></div>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
