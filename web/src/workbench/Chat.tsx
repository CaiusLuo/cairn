import { useEffect, useRef, useState } from "react";
import { runtimeApi } from "../runtime-api";
import { readSubmission, sendSubmission, useRunStream, type Submission } from "./chat-state";
import { isActive, sourceLabel, useResource, type Conversation, type Message, type Run, type WorkbenchConfig } from "./data";
import { Badge, Drawer, Markdown, Outputs, Timeline } from "./evidence";
import { CairnMark, Icon } from "./Icon";
import { BrandIntro, shortcuts } from "./Home";
import { href } from "./Shell";
import { useEventLog } from "./execution";

function SettledTrace({ id }: { id: string }) {
  const log = useEventLog(`runs/${encodeURIComponent(id)}`, false);
  return log.rows.length ? <details className="wb-process"><summary>执行轨迹 · {log.rows.length} 条活动</summary><Timeline events={log.rows} compact/></details> : null;
}

type Queued = { id: string; prompt: string; model?: string };

export function Chat({ id, initialMode, config, navigate, onSubmit }: {
  id: string; initialMode: Submission["mode"]; config?: WorkbenchConfig; navigate: (url: string) => void; onSubmit: () => void;
}) {
  const key = `cairn.web.pending.${config?.workspace ?? "workspace"}:${id || "new"}`;
  const detail = useResource<{ conversation: Conversation; runs: Run[] }>(id ? `conversations/${encodeURIComponent(id)}` : null, 1200);
  const observed = detail.data?.runs.at(-1);
  const [acknowledged, setAcknowledged] = useState<Run>();
  const run = acknowledged && observed?.run_id !== acknowledged.run_id &&
    (!observed?.created_at || (acknowledged.created_at ?? "") > observed.created_at) ? acknowledged : observed;
  const active = Boolean(run && isActive(run.status));
  const history = useResource<Message[]>(id ? `conversations/${encodeURIComponent(id)}/history` : null);
  const stream = useRunStream(run);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<Submission | null>(null);
  const [model, setModel] = useState("");
  const [mode, setMode] = useState<Submission["mode"]>(initialMode);
  const [queue, setQueue] = useState<Queued[]>([]);
  const [auto, setAuto] = useState(true);
  const [sending, setSending] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [error, setError] = useState("");
  const [artifacts, setArtifacts] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const tail = useRef<HTMLDivElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { setPending(readSubmission(key)); }, [key]);
  useEffect(() => { if (id && !active) history.refresh(); }, [id, active, run?.run_id, history.refresh]);
  useEffect(() => {
    if (stickToBottom.current) tail.current?.scrollIntoView({ block: "end" });
  }, [history.data, stream.preview, run?.status]);

  async function submit(next: Submission) {
    if (inFlight.current || !config) return;
    inFlight.current = true;
    setSending(true);
    setError("");
    setPending(next);
    try {
      const result = await sendSubmission(next, key);
      onSubmit();
      if (!mounted.current) return;
      setPending(null);
      if ("task" in result && result.task) {
        navigate(href("tasks", { task: result.task.task_id }));
      } else if (result.run) {
        if (result.run.conversation_id !== id) navigate(href("chat", { conversation: result.run.conversation_id }));
        else { setAcknowledged(result.run); detail.refresh(); }
      }
    } catch (reason) {
      if (mounted.current) setError(String(reason));
    } finally {
      inFlight.current = false;
      if (mounted.current) setSending(false);
    }
  }

  function sendQueued(item: Queued) {
    setQueue(rows => rows.filter(row => row.id !== item.id));
    void submit({ ...item, conversation: id, mode: "conversation" });
  }

  useEffect(() => {
    if (auto && queue[0] && run?.status === "completed" && !pending && !sending && config) sendQueued(queue[0]);
  });

  function sendDraft() {
    const prompt = draft.trim();
    if (!prompt || pending || sending || !config || (failed && mode === "conversation")) return;
    const next = { id: crypto.randomUUID(), prompt, model: mode === "conversation" && model ? model : undefined };
    setDraft("");
    stickToBottom.current = true;
    if (active && mode === "conversation") setQueue(rows => [...rows, next]);
    else void submit({ ...next, conversation: id, mode });
  }

  async function cancel() {
    if (!run || !active || canceling) return;
    setAuto(false);
    setCanceling(true);
    setError("");
    try {
      await runtimeApi(`runs/${encodeURIComponent(run.run_id)}/cancel`, {});
      detail.refresh();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setCanceling(false);
    }
  }

  const failed = run?.status === "recovery_required";
  const title = detail.data?.conversation.initial_prompt;
  const messages = (history.data ?? []).filter(message => ["user", "assistant"].includes(message.role) && message.content);
  const form = <div className="composer-stack">
    {!!queue.length && <div className="queued-tray">
      {!auto && <p className="wb-muted">排队消息已暂停自动发送。</p>}
      {queue.map(item => <div key={item.id} className="queued-line">
        <Icon name="queue" size={15}/><span>{item.prompt}</span>
        <button disabled={active || sending || !!pending || failed} onClick={() => sendQueued(item)}>发送</button>
        <button onClick={() => { setDraft(item.prompt); setQueue(rows => rows.filter(row => row.id !== item.id)); }}>编辑</button>
        <button aria-label="删除排队消息" onClick={() => setQueue(rows => rows.filter(row => row.id !== item.id))}><Icon name="trash" size={14}/></button>
      </div>)}
    </div>}
    {pending && <div className="submission-review" role="status"><span>待确认请求 · {pending.prompt.slice(0, 100)}</span><button disabled={sending} onClick={() => void submit(pending)}>用原编号重试</button><button disabled={sending} onClick={() => { sessionStorage.removeItem(key); setPending(null); detail.refresh(); }}>清除本地记录</button></div>}
    {(error || detail.error || history.error) && <p className="wb-error" role="alert">{error || detail.error || history.error}</p>}
    <form className="wb-composer" onSubmit={event => { event.preventDefault(); sendDraft(); }}>
      <textarea aria-label="消息" placeholder={mode === "development" ? "描述要完成的开发目标…" : "问 Cairn，或交给它一项工作…"} value={draft} maxLength={32000} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); sendDraft(); } }}/>
      <div className="composer-controls">
        <span className="composer-harness"><Icon name="sparkles" size={15}/>Cairn Agent</span>
        <label><Icon name="server" size={15}/><select aria-label="模型" disabled={mode === "development" || !config?.models.length} value={mode === "development" ? "" : model} onChange={event => setModel(event.target.value)}>
          <option value="">{mode === "development" ? config?.development_model ?? "Server 默认模型" : config?.conversation_model ?? "离线验收模型"}</option>
          {config?.models.map(item => <option key={item} value={item}>{item}</option>)}
        </select></label>
        <label title={mode === "development" ? `Shell ${config?.development_shell ? "宿主已授权" : "关闭"} · 网络 ${config?.development_network ? "宿主已授权" : "关闭"}` : "对话中的项目文件只读"}><Icon name="lock" size={15}/><select aria-label="权限模式" value={mode} onChange={event => setMode(event.target.value as Submission["mode"])}>
          <option value="conversation">对话 · 项目只读</option><option value="development">开发 · 独立工作树</option>
        </select></label>
        <div className="composer-send">
          {active && <button className="composer-icon" type="button" aria-label="中断运行" title="中断运行" disabled={canceling} onClick={() => void cancel()}><Icon name="stop" size={17}/></button>}
          <button className="send-button" type="submit" aria-label={active && mode === "conversation" ? "加入发送队列" : "发送"} title={active && mode === "conversation" ? "加入发送队列" : "发送"} disabled={!draft.trim() || !config || sending || !!pending || (failed && mode === "conversation")}><Icon name={active && mode === "conversation" ? "queue" : "arrow-up"} size={18}/></button>
        </div>
      </div>
    </form>
  </div>;

  if (!id) return <div className="wb-home"><div className="wb-home-inner"><BrandIntro/>{form}<div className="wb-shortcuts">{shortcuts.map(item => <a key={item.title} href={item.href} onClick={event => { event.preventDefault(); navigate(item.href); }}><span><Icon name={item.icon}/></span><strong>{item.title}</strong><small>{item.detail}</small></a>)}</div></div></div>;
  return <section className="wb-chat">
    <header className="wb-chat-header">
      <div><h1>{title ?? "对话"}</h1>{detail.data && <small>{sourceLabel(detail.data.conversation.source)}</small>}</div>
      {run && <><Badge status={run.status}/><button title="查看运行详情" aria-label="查看运行详情" onClick={() => navigate(href("runs", { run: run.run_id }))}><Icon name="runs" size={17}/></button><button title="产物" aria-label="产物" onClick={() => setArtifacts(true)}><Icon name="chat-artifacts" size={17}/></button></>}
    </header>
    <div className="wb-transcript" ref={transcript} onScroll={() => { const element = transcript.current; if (element) stickToBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 160; }}>
      <div className="wb-transcript-inner">
        {!detail.data && !detail.error && <p className="wb-muted">正在读取对话…</p>}
        {messages.map((message, index) => <article key={index} className={`wb-message ${message.role}`}>
          {message.role === "assistant" && <CairnMark size={22}/>}<Markdown text={message.content!}/>
        </article>)}
        {active && run?.prompt && (messages.at(-1)?.role !== "user" || messages.at(-1)?.content !== run.prompt) && <article className="wb-message user"><Markdown text={run.prompt}/></article>}
        {active && <article className="wb-message assistant" aria-live="polite"><CairnMark size={22}/><div>
          {stream.events.length > 0 && <details className="wb-process" open={!stream.preview}><summary>正在处理 · {stream.events.length} 条活动</summary><Timeline events={stream.events} compact/></details>}
          {stream.preview ? <Markdown text={stream.preview}/> : <p className="wb-muted">{run?.status === "queued" ? "等待 Server 执行…" : "Cairn 正在处理…"}</p>}
        </div></article>}
        {!active && run && <SettledTrace key={run.run_id} id={run.run_id}/>}
        {!active && run?.error && <p className="wb-error">{run.error}{failed && <> · <a href={href("approvals", { run: run.run_id })}>打开恢复核对</a></>}</p>}
        {run?.task_id && <a className="wb-inline-link" href={href("tasks", { task: run.task_id })}>打开开发任务 <Icon name="arrow-right" size={14}/></a>}
        <div ref={tail}/>
      </div>
    </div>
    <div className="wb-chat-compose">{form}</div>
    {artifacts && <Drawer title="对话产物" close={() => setArtifacts(false)}><Outputs result={run?.result} history={history.data ?? []}/></Drawer>}
  </section>;
}
