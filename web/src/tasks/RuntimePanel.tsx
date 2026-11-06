import { useEffect, useRef, useState } from "react";
import { runtimeApi as api, RuntimeError, taskStatus } from "../runtime-api";

type Task = { session_id: string | null; prompt: string; sequence: number; mode: "conversation" | "development"; task_id: string; status: string; worktree: string | null; result: string | null; diagnostic: string | null; metrics: Record<string, unknown> };
type Message = { role: string; content?: string | null; tool_calls?: unknown[] };
type Recovery = { expected_sequence: number; revision: unknown; action: string; fences: unknown[]; checkpoint_outcome: string | null };

export function RuntimePanel() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [active, setActive] = useState(new URLSearchParams(location.search).get("task") || "");
  const [history, setHistory] = useState<Message[]>([]);
  const [preview, setPreview] = useState("");
  const [recovery, setRecovery] = useState<Recovery | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [note, setNote] = useState("");
  const [mode, setMode] = useState<"conversation" | "development">("conversation");
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(false);
  const pending = useRef<{ prompt: string; id: string; target: string; sequence: number } | null>(null);
  const selected = tasks.find(task => task.task_id === active);
  const activeRef = useRef(active); activeRef.current = active;
  useEffect(() => {
    let live = true;
    const refresh = async () => {
      try { const data: Task[] = await api("tasks"); if (live) { setTasks(data); setConnected(true); } }
      catch (reason) { if (live) { setConnected(false); setError(String(reason)); } }
    };
    void refresh(); const timer = setInterval(() => void refresh(), 2000);
    return () => { live = false; clearInterval(timer); };
  }, []);
  useEffect(() => {
    setHistory([]); setPreview(""); setRecovery(null); setReviewed(false); setNote("");
    const url = new URL(location.href); active ? url.searchParams.set("task", active) : url.searchParams.delete("task"); window.history.replaceState(null, "", url);
  }, [active]);
  const historyVersion = selected && ["running", "queued"].includes(selected.status) ? selected.session_id : selected?.sequence;
  useEffect(() => {
    if (!active || !selected?.session_id) return;
    let live = true;
    void api(`tasks/${active}/history`).then(data => { if (live) setHistory(data); }).catch(reason => { if (live) setError(String(reason)); });
    return () => { live = false; };
  }, [active, historyVersion]);
  useEffect(() => {
    if (!active) return;
    let live = true, cursor = 0;
    let socket: WebSocket | undefined;
    let retry: ReturnType<typeof setTimeout>;
    const connect = () => {
      socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/runtime/tasks/${active}/stream?after=${cursor}`);
      socket.onmessage = message => {
        if (!live) return;
        const data = JSON.parse(message.data);
        if (data.type === "event" && data.event.sequence > cursor) { cursor = data.event.sequence; if (["running", "interrupted", "failed", "paused", "completed", "model_stream_start"].includes(data.event.kind)) setPreview(""); }
        else if (data.type === "preview") setPreview(old => (old + data.text).slice(-64000));
        else if (data.type === "settled") setPreview("");
      };
      socket.onclose = () => { if (live) retry = setTimeout(connect, 2000); };
    };
    connect(); return () => { live = false; clearTimeout(retry); socket?.close(); };
  }, [active]);
  async function submit() {
    if (!prompt.trim() || busy) return;
    if (pending.current && (pending.current.prompt !== prompt || pending.current.target !== active)) { setError("上次发送结果尚未确认。请保持原内容重试, 或先按任务列表核对实际状态。"); return; }
    pending.current ??= { prompt, id: crypto.randomUUID(), target: active, sequence: selected?.sequence ?? 0 };
    setBusy(true); setError("");
    try {
      const task: Task = selected ? await api(`tasks/${active}/messages`, { prompt, source: "web", request_id: pending.current.id, expected_sequence: pending.current.sequence }) : await api("tasks", { prompt, source: "web", request_id: pending.current.id, mode });
      setTasks(old => [...old.filter(item => item.task_id !== task.task_id), task]); setActive(task.task_id); setPrompt(""); pending.current = null;
    } catch (reason) { if (reason instanceof RuntimeError && reason.status >= 400 && reason.status < 500) pending.current = null; setError(String(reason)); }
    finally { setBusy(false); }
  }
  async function action(path: string, body: unknown = {}) {
    setBusy(true); setError(""); const target = active;
    try { const task: Task = await api(`tasks/${target}/${path}`, body); setTasks(old => old.map(item => item.task_id === task.task_id ? task : item)); if (activeRef.current === target) { setRecovery(null); setReviewed(false); setNote(""); } }
    catch (reason) { setError(String(reason)); } finally { setBusy(false); }
  }
  async function inspectRecovery() {
    const target = active; setBusy(true); setError("");
    try { const report = await api(`tasks/${target}/recovery`); if (activeRef.current === target) { setRecovery(report); setReviewed(false); } }
    catch (reason) { setError(String(reason)); } finally { setBusy(false); }
  }
  return <div className="chat-layout">
    <aside className="conversations" aria-label="共享对话列表">
      <button className="primary" disabled={busy || pending.current !== null} onClick={() => setActive("")}>＋ 新对话</button>
      <p className="muted">CLI、Web、微信共用</p>
      {tasks.slice().reverse().map(task => <button key={task.task_id} className={`conversation${task.task_id === active ? " selected" : ""}`} disabled={busy || pending.current !== null} onClick={() => setActive(task.task_id)}><span>{task.prompt.slice(0, 70)}</span><small>{taskStatus(task.status)}</small></button>)}
      {connected && !tasks.length && <p className="muted">还没有对话</p>}
    </aside>
    <section className="chat-thread" aria-label="对话">
      <header className="chat-header"><div><h1>{selected ? selected.prompt.slice(0, 50) : "开始对话"}</h1><span className="muted">{selected ? `${selected.task_id} · ${taskStatus(selected.status)}` : "同一个 Agent, 从聊天到项目开发"}</span></div><a href="/settings.html">微信与设置 →</a></header>
      <div className="messages" aria-live="polite">
        {!selected && <div className="chat-empty"><h2>今天想完成什么？</h2><p>直接聊天, 或把开发目标交给 Cairn。</p></div>}
        {history.filter(message => message.content || message.tool_calls?.length).map((message, index) => message.role === "tool" || message.tool_calls?.length ? <details className="tool-message" key={index}><summary>工具记录 · {message.role}</summary><pre>{message.content || JSON.stringify(message.tool_calls, null, 2)}</pre></details> : <article className={`message message--${message.role}`} key={index}><small>{message.role === "user" ? "你" : message.role === "assistant" ? "Cairn" : message.role}</small><div>{message.content}</div></article>)}
        {selected && !history.length && <article className="message message--user"><small>你</small><div>{selected.prompt}</div></article>}
        {preview && <article className="message"><small>Cairn · 正在回复</small><div>{preview}</div></article>}
        {selected?.diagnostic && <p role="status">{selected.diagnostic}</p>}
        {selected && <details className="task-details"><summary>任务信息与恢复</summary><p>Worktree: {selected.worktree || "普通聊天, 尚未创建开发目录"}</p><pre>{JSON.stringify(selected.metrics, null, 2)}</pre><button disabled={busy || selected.status !== "paused"} onClick={() => void action("continue")}>继续任务</button><button disabled={busy || !["queued", "running"].includes(selected.status)} onClick={() => void action("cancel")}>取消任务</button>{selected.status === "recovery_required" && <div><button disabled={busy} onClick={() => void inspectRecovery()}>查看恢复证据</button>{recovery && <><pre>{JSON.stringify(recovery, null, 2)}</pre><label><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} />已核对工作目录及可能发生的操作</label><label>核对结果<textarea value={note} onChange={event => setNote(event.target.value)} maxLength={1024} /></label><button disabled={busy || !reviewed || !note.trim()} onClick={() => void action("reconcile", { expected_sequence: recovery.expected_sequence, revision: recovery.revision, action: recovery.action, reviewed_effects: true, note })}>确认状态, 不重放操作</button></>}</div>}</details>}
      </div>
      <form className="chat-composer" onSubmit={event => { event.preventDefault(); void submit(); }}>
        {error && <p role="alert" className="control-error">{error}</p>}
        {!selected && <label className="mode-label">模式 <select value={mode} onChange={event => setMode(event.target.value as "conversation" | "development")}><option value="conversation">聊天 · 需要时进入开发</option><option value="development">直接开发任务</option></select></label>}
        <label className="sr-only" htmlFor="chat-input">发送消息</label><textarea id="chat-input" placeholder={selected && ["running", "queued"].includes(selected.status) ? "任务处理中, 完成后可继续对话" : "输入消息…"} value={prompt} onChange={event => setPrompt(event.target.value)} maxLength={32000} rows={3} onKeyDown={event => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void submit(); } }} />
        <div className="composer-foot"><small>Ctrl / ⌘ + Enter 发送 · 关闭页面不会停止任务</small><button className="primary" disabled={busy || !connected || !prompt.trim() || Boolean(selected && !["completed", "paused"].includes(selected.status))}>发送</button></div>
      </form>
    </section>
  </div>;
}
