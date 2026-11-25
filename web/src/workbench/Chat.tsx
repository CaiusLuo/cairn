import { useEffect, useRef, useState, type RefObject } from "react";
import { newRequestId } from "../ids";
import { RuntimeError, runtimeApi, uploadAttachment, type UploadedAttachment } from "../runtime-api";
import { readSubmission, sendSubmission, useRunStream, type Submission } from "./chat-state";
import { conversationTitle, isActive, modelKey, parseModelKey, sourceLabel, useResource, type Conversation, type KnowledgeSource, type Message, type Run, type WorkbenchConfig } from "./data";
import { Badge, Drawer, Markdown, Outputs, Timeline } from "./evidence";
import { CairnMark } from "./Brand";
import { Icon } from "./Icon";
import { KnowledgeSources } from "./KnowledgeSources";
import { BrandIntro, shortcuts } from "./Home";
import { href } from "./Shell";
import { useEventLog } from "./execution";

function SettledTrace({ id }: { id: string }) {
  const log = useEventLog(`runs/${encodeURIComponent(id)}`, false);
  return log.rows.length ? <details className="wb-process"><summary>执行轨迹 · {log.rows.length} 条活动</summary><Timeline events={log.rows} compact/></details> : null;
}

// Plain HTTP pages have no navigator.clipboard, so keep the textarea fallback.
function legacyCopy(text: string) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.append(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
  }
}

function CopyButton({ text }: { text: string }) {
  const [status, setStatus] = useState<"copied" | "failed" | null>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  function flashStatus(next: "copied" | "failed") {
    setStatus(next);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setStatus(null), 1800);
  }
  function fallbackCopy() {
    flashStatus(legacyCopy(text) ? "copied" : "failed");
  }
  function copy() {
    if (!navigator.clipboard?.writeText) return fallbackCopy();
    navigator.clipboard.writeText(text).then(() => flashStatus("copied"), fallbackCopy);
  }
  const label = status === "copied" ? "已复制" : status === "failed" ? "复制失败" : "复制回答";
  return <button type="button" className="icon-button wb-message-copy" data-status={status ?? undefined} aria-label={label} title={label} onClick={copy}>
    {status === "copied"
      ? <Icon name="check" size={14}/>
      : <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect width="12" height="12" x="9" y="9" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>}
  </button>;
}

type SteerPhase = "sending" | "accepted" | "applied" | "queued";
type Steering = { id: string; text: string; phase: SteerPhase; after: number; pending?: number };

const steerLabel = (steer: Steering) =>
  steer.phase === "sending" ? "正在加入当前运行…"
    : steer.phase === "accepted" ? (steer.pending ?? 0) > 1 ? `已加入当前运行 · ${steer.pending} 条待处理` : "已加入当前运行，下一步生效"
    : steer.phase === "applied" ? "引导已生效" : "运行已结束，已放入发送队列";

function useSteerAnimation(ref: RefObject<HTMLDivElement | null>, phase: SteerPhase | null) {
  useEffect(() => {
    let cancelled = false;
    let media: { revert: () => void } | undefined;
    void import("gsap").then(({ gsap }) => {
      if (cancelled || !ref.current) return;
      const context = gsap.matchMedia();
      media = context;
      context.add("(prefers-reduced-motion: no-preference)", () => {
        const element = ref.current;
        if (!element) return;
        gsap.fromTo(element, { y: 5, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.22, ease: "power2.out", clearProps: "transform,opacity,visibility" });
        if (phase === "applied") gsap.fromTo(element.querySelector(".steer-dot"), { scale: 0.4 }, { scale: 1, duration: 0.36, ease: "back.out(2.4)", clearProps: "transform" });
      });
    });
    return () => { cancelled = true; media?.revert(); };
  }, [ref, phase]);
}

type Queued = Pick<Submission, "id" | "prompt" | "model" | "provider" | "group" | "attachment_ids" | "attachments">;

function modelOptions(config?: WorkbenchConfig) {
  return (config?.providers ?? []).flatMap(provider =>
    provider.groups.flatMap(group =>
      group.model_ids.map(model => ({
        key: modelKey(provider.name, group.name, model),
        provider: provider.name,
        group: group.name,
        model,
      })),
    ),
  );
}

const sizeLabel = (bytes: number) => bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MiB` : `${Math.max(1, Math.round(bytes / 1024))} KiB`;
const attachmentNames = (items?: { name: string }[]) => items?.map(item => item.name).join(" · ");

export function Chat({ id, initialMode, config, navigate, onSubmit }: {
  id: string; initialMode: Submission["mode"]; config?: WorkbenchConfig; navigate: (url: string) => void; onSubmit: () => void;
}) {
  const key = `cairn.web.pending.${config?.workspace ?? "workspace"}:${id || "new"}`;
  const detail = useResource<{ conversation: Conversation; runs: Run[] }>(id ? `conversations/${encodeURIComponent(id)}` : null, 1200);
  const observed = detail.data?.runs?.at(-1);
  const [acknowledged, setAcknowledged] = useState<Run>();
  const run = acknowledged && observed?.run_id !== acknowledged.run_id &&
    (!observed?.created_at || (acknowledged.created_at ?? "") > observed.created_at) ? acknowledged : observed;
  const active = Boolean(run && isActive(run.status));
  const history = useResource<Message[]>(id ? `conversations/${encodeURIComponent(id)}/history?include=knowledge` : null);
  const stream = useRunStream(run);
  const [draft, setDraft] = useState("");
  const [draftSource, setDraftSource] = useState<(KnowledgeSource & { offset: number }) | null>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const [pending, setPending] = useState<Submission | null>(null);
  const [model, setModel] = useState("");
  const [mode, setMode] = useState<Submission["mode"]>(initialMode);
  const [queue, setQueue] = useState<Queued[]>([]);
  const [auto, setAuto] = useState(true);
  const [sending, setSending] = useState(false);
  const [steering, setSteering] = useState(false);
  const [steer, setSteer] = useState<Steering | null>(null);
  const [canceling, setCanceling] = useState(false);
  const [attachments, setAttachments] = useState<UploadedAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [artifacts, setArtifacts] = useState(false);
  const [, setRetryTick] = useState(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  const retryAt = useRef(0);
  const mounted = useRef(true);
  const tail = useRef<HTMLDivElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const steerRef = useRef<HTMLDivElement>(null);
  useSteerAnimation(steerRef, steer?.phase ?? null);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { setPending(readSubmission(key)); }, [key]);
  useEffect(() => { setSteer(null); setDraftSource(null); }, [id]);
  useEffect(() => {
    if (model && config && !modelOptions(config).some(option => option.key === model)) setModel("");
  }, [config, model]);
  useEffect(() => {
    if (!steer || steer.phase === "queued" || steer.phase === "applied") return;
    const match = [...stream.events].reverse().find(event =>
      (event.kind === "steer_applied" || event.kind === "steer_unapplied") &&
      event.sequence > steer.after && event.data.text === steer.text);
    if (!match) return;
    if (match.kind === "steer_applied") {
      setSteer(current => current?.id === steer.id ? { ...current, phase: "applied" } : current);
    } else {
      setQueue(rows => rows.some(row => row.id === steer.id) ? rows : [...rows, { id: steer.id, prompt: steer.text }]);
      setSteer(current => current?.id === steer.id ? { ...current, phase: "queued" } : current);
    }
  }, [stream.events, steer]);
  useEffect(() => { if (id && !active) history.refresh(); }, [id, active, run?.run_id, history.refresh]);
  useEffect(() => {
    if (stickToBottom.current) tail.current?.scrollIntoView({ block: "end" });
  }, [history.data, stream.preview, run?.status]);

  async function submit(next: Submission) {
    if (inFlight.current || !config) return;
    inFlight.current = true;
    setSending(true);
    setError("");
    setSteer(null);
    setPending(next);
    try {
      const result = await sendSubmission(next, key);
      onSubmit();
      if (!mounted.current) return;
      setPending(null);
      if ("task" in result && result.task) {
        navigate(href("tasks", { task: result.task.task_id }));
      } else if (result.run) {
        if (result.run.conversation_id !== id) {
          navigate(href("chat", { conversation: result.run.conversation_id }));
        } else { setAcknowledged(result.run); detail.refresh(); }
      }
    } catch (reason) {
      if (!mounted.current) return;
      if (reason instanceof RuntimeError && reason.status === 409 && reason.message === "conversation_busy") {
        sessionStorage.removeItem(key);
        setPending(null);
        setQueue(rows => rows.some(row => row.id === next.id) ? rows : [...rows, next]);
        retryAt.current = Date.now() + 1200;
        window.setTimeout(() => { if (mounted.current) setRetryTick(value => value + 1); }, 1300);
      } else {
        setError(String(reason));
      }
    } finally {
      inFlight.current = false;
      if (mounted.current) setSending(false);
    }
  }

  function sendQueued(item: Queued) {
    if (inFlight.current || pending || !config) return;
    setQueue(rows => rows.filter(row => row.id !== item.id));
    void submit({ ...item, conversation: id, mode: "conversation" });
  }

  async function attachFiles(files: FileList | null) {
    if (!files?.length || mode === "development") return;
    setUploading(true);
    setError("");
    const uploaded: UploadedAttachment[] = [];
    try {
      const room = Math.max(0, 5 - attachments.length);
      for (const file of Array.from(files).slice(0, room)) uploaded.push(await uploadAttachment(file));
    } catch (reason) {
      if (mounted.current) setError(String(reason));
    } finally {
      if (fileInput.current) fileInput.current.value = "";
      if (mounted.current) {
        if (uploaded.length) setAttachments(current => [...current, ...uploaded]);
        setUploading(false);
      }
    }
  }

  useEffect(() => {
    if (auto && queue[0] && run?.status === "completed" && !sending && !pending && config && Date.now() >= retryAt.current) sendQueued(queue[0]);
  });

  async function steerPrompt(prompt: string) {
    if (!run || steering) return;
    const next: Steering = { id: newRequestId(), text: prompt, phase: "sending", after: stream.events.at(-1)?.sequence ?? 0 };
    setDraft("");
    stickToBottom.current = true;
    setError("");
    setSteering(true);
    setSteer(next);
    try {
      const receipt = await runtimeApi<{ pending: number }>(`runs/${encodeURIComponent(run.run_id)}/steer`, { prompt });
      if (!mounted.current) return;
      setSteer(current => current?.id === next.id && current.phase === "sending" ? { ...current, phase: "accepted", pending: receipt.pending } : current);
    } catch (reason) {
      if (!mounted.current) return;
      if (reason instanceof RuntimeError && reason.status === 409) {
        setQueue(rows => rows.some(row => row.id === next.id) ? rows : [...rows, { id: next.id, prompt }]);
        setSteer(current => current?.id === next.id ? { ...current, phase: "queued" } : current);
      } else {
        setDraft(prompt);
        setSteer(current => current?.id === next.id ? null : current);
        setError(String(reason));
      }
    } finally {
      if (mounted.current) setSteering(false);
    }
  }

  function sendDraft() {
    const text = draft.trim();
    if (!text || inFlight.current || sending || steering || uploading || !config || (failed && mode === "conversation")) return;
    const prompt = draftSource
      ? text + "\n\n引用资料（请用 document_read 核对）：document_id=" + draftSource.document_id
        + "，chunk_id=" + draftSource.chunk_id + "，offset=" + draftSource.offset
      : text;
    if (prompt.length > 32000) {
      setError("请缩短消息，给来源引用留出空间。");
      return;
    }
    if (pending) {
      sessionStorage.removeItem(key);
      setPending(null);
    }
    const attachment_ids = attachments.map(item => item.attachment_id);
    const choice = mode === "conversation" && model ? parseModelKey(model) : null;
    const next = {
      id: newRequestId(), prompt, ...(choice ?? {}),
      ...(attachment_ids.length ? { attachment_ids, attachments } : {}),
    };
    setDraft("");
    setDraftSource(null);
    setAttachments([]);
    stickToBottom.current = true;
    if (steerable) void steerPrompt(prompt);
    else if (active && mode === "conversation") setQueue(rows => [...rows, next]);
    else void submit({ ...next, conversation: id, mode });
  }

  function askAboutSource(source: KnowledgeSource & { offset: number }) {
    setMode("conversation");
    setDraftSource(source);
    setDraft(current => current.trim() ? current : "请解释这段资料，并给出具体建议。");
    composerInput.current?.focus();
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
  const steerable = active && run?.status === "running" && mode === "conversation" && attachments.length === 0 && !draftSource;
  const degraded = (detail.data?.runs ?? [])
    .map(item => item.metadata?.history_degraded)
    .filter((value): value is string => typeof value === "string")
    .at(-1);
  const title = detail.data?.conversation.title || detail.data?.conversation.initial_prompt;
  const messages = (history.data ?? []).filter(message => ["user", "assistant"].includes(message.role) && message.content);
  const form = <div className="composer-stack">
    {!!queue.length && <div className="queued-tray">
      {!auto && <p className="wb-muted">排队消息已暂停自动发送。</p>}
      {queue.map(item => <div key={item.id} className="queued-line">
        <Icon name="queue" size={15}/><span>{item.prompt}{item.attachments?.length ? ` · ${attachmentNames(item.attachments)}` : ""}</span>
        <button disabled={active || sending || !!pending || failed} onClick={() => sendQueued(item)}>发送</button>
        <button disabled={!!draft.trim() || uploading || !!attachments.length || (!!item.attachment_ids?.length && !item.attachments)} onClick={() => {
          setDraft(item.prompt);
          setDraftSource(null);
          setMode("conversation");
          setModel(item.provider && item.group && item.model ? modelKey(item.provider, item.group, item.model) : modelOptions(config).find(option => option.model === item.model)?.key ?? "");
          setAttachments(item.attachments ?? []);
          setQueue(rows => rows.filter(row => row.id !== item.id));
        }}>编辑</button>
        <button aria-label="删除排队消息" onClick={() => setQueue(rows => rows.filter(row => row.id !== item.id))}><Icon name="trash" size={14}/></button>
      </div>)}
    </div>}
    {pending && <div className="submission-review" role="status"><span>待确认请求 · {pending.prompt.slice(0, 100)}（发送新消息会放弃这条记录）</span><button disabled={sending} onClick={() => void submit(pending)}>用原编号重试</button><button disabled={sending} onClick={() => { sessionStorage.removeItem(key); setPending(null); detail.refresh(); }}>放弃这条记录</button></div>}
    {steer && <div className="steer-status" role="status" data-phase={steer.phase} ref={steerRef} key={steer.phase}>
      <i className="steer-dot" aria-hidden="true"/><span>{steerLabel(steer)}</span><small title={steer.text}>{steer.text}</small>
    </div>}
    {(error || detail.error || history.error) && <p className="wb-error wb-error-action" role="alert">
      <span>{error || detail.error || history.error}</span>
      {!error && <button type="button" onClick={() => { detail.refresh(); history.refresh(); }}>重试</button>}
    </p>}
    <form className="wb-composer" onSubmit={event => { event.preventDefault(); sendDraft(); }}>
      {draftSource && <div className="attachment-tray">
        <span className="attachment-chip">
          <Icon name="doc" size={14}/><span>引用 {draftSource.title}</span>
          <button type="button" aria-label="移除知识库引用" onClick={() => setDraftSource(null)}><Icon name="close" size={12}/></button>
        </span>
      </div>}
      {!!attachments.length && <div className="attachment-tray">
        {attachments.map(item => <span className="attachment-chip" key={item.attachment_id}>
          <Icon name="doc" size={14}/><span title={item.name}>{item.name}</span><small>{sizeLabel(item.size)}</small>
          <button type="button" aria-label={`移除 ${item.name}`} onClick={() => setAttachments(rows => rows.filter(row => row.attachment_id !== item.attachment_id))}><Icon name="close" size={12}/></button>
        </span>)}
      </div>}
      <textarea ref={composerInput} aria-label="消息" placeholder={mode === "development" ? "描述要完成的开发目标…" : steerable ? "输入以引导当前运行…" : "问 Cairn，或交给它一项工作…"} value={draft} maxLength={32000} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); sendDraft(); } }}/>
      <div className="composer-controls">
        <span className="composer-harness"><Icon name="sparkles" size={15}/>Cairn Agent</span>
        <input ref={fileInput} type="file" multiple hidden onChange={event => void attachFiles(event.target.files)}/>
        <button className="composer-icon" type="button" aria-label="添加附件" title="添加附件（文本、PDF、Office 或 HTML）" disabled={mode === "development" || uploading || steering || attachments.length >= 5} onClick={() => fileInput.current?.click()}><Icon name={uploading ? "clock" : "doc"} size={16}/></button>
        <label><Icon name="server" size={15}/><select aria-label="模型" disabled={mode === "development" || !modelOptions(config).length} value={mode === "development" ? "" : model} onChange={event => setModel(event.target.value)}>
          <option value="">{mode === "development" ? config?.development_model ?? "Server 默认模型" : config?.active_model ? `${config.active_provider} / ${config.active_group} / ${config.active_model}` : config?.conversation_model ?? "离线验收模型"}</option>
          {(config?.providers ?? []).map(provider => provider.groups.map(group => <optgroup key={`${provider.name}:${group.name}`} label={`${provider.name} · ${group.name}`}>
            {group.model_ids.map(item => <option key={modelKey(provider.name, group.name, item)} value={modelKey(provider.name, group.name, item)}>{item}</option>)}
          </optgroup>))}
        </select></label>
        <label title={mode === "development" ? `Shell ${config?.development_shell ? "宿主已授权" : "关闭"} · 网络 ${config?.development_network ? "宿主已授权" : "关闭"}` : "对话中的项目文件只读"}><Icon name="lock" size={15}/><select aria-label="权限模式" value={mode} onChange={event => setMode(event.target.value as Submission["mode"])}>
          <option value="conversation">对话 · 项目只读</option><option value="development" disabled={uploading || attachments.length > 0 || !!draftSource}>开发 · 独立工作树</option>
        </select></label>
        <div className="composer-send">
          {active && <button className="composer-icon" type="button" aria-label="中断运行" title="中断运行" disabled={canceling} onClick={() => void cancel()}><Icon name="stop" size={17}/></button>}
          <button className="send-button" type="submit" aria-label={steerable ? "引导当前运行" : active && mode === "conversation" ? "加入发送队列" : "发送"} title={steerable ? "引导当前运行" : active && mode === "conversation" ? "加入发送队列" : "发送"} disabled={!draft.trim() || !config || sending || steering || uploading || (failed && mode === "conversation")}><Icon name={steerable ? "sparkles" : active && mode === "conversation" ? "queue" : "arrow-up"} size={18}/></button>
        </div>
      </div>
    </form>
  </div>;

  if (!id) return <div className="wb-home"><div className="wb-home-inner"><BrandIntro/>{form}<div className="wb-shortcuts">{shortcuts.map(item => <a key={item.title} href={item.href} onClick={event => { event.preventDefault(); navigate(item.href); }}><span><Icon name={item.icon}/></span><strong>{item.title}</strong></a>)}</div></div></div>;
  return <section className="wb-chat">
    <header className="wb-chat-header">
      <div><h1 title={title}>{conversationTitle(title)}</h1>{detail.data && <small>{sourceLabel(detail.data.conversation.source)}</small>}</div>
      {run && <><Badge status={run.status}/><button title="查看运行详情" aria-label="查看运行详情" onClick={() => navigate(href("runs", { run: run.run_id }))}><Icon name="runs" size={17}/></button><button title="产物" aria-label="产物" onClick={() => setArtifacts(true)}><Icon name="chat-artifacts" size={17}/></button></>}
    </header>
    {degraded && <p className="wb-muted" role="status">旧 Session 记录不可用，已在新 Session 继续；旧操作不会重放。</p>}
    <div className="wb-transcript" ref={transcript} onScroll={() => { const element = transcript.current; if (element) stickToBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 160; }}>
      <div className="wb-transcript-inner">
        {!detail.data && !detail.error && <p className="wb-muted">正在读取对话…</p>}
        {messages.map((message, index) => {
          const content = message.knowledge ? <div className="wb-message-with-sources">
            <Markdown text={message.content!}/>
            <KnowledgeSources card={message.knowledge} onAsk={askAboutSource}/>
          </div> : <Markdown text={message.content!}/>;
          return <article key={message.knowledge?.run_id ?? index} className={`wb-message ${message.role}`}>
            {message.role === "assistant" && <CairnMark size={22}/>}
            {message.role === "assistant" ? <div className="wb-message-body">{content}<CopyButton text={message.content!}/></div> : content}
          </article>;
        })}
        {active && run?.prompt && (messages.at(-1)?.role !== "user" || messages.at(-1)?.content !== run.prompt) && <article className="wb-message user"><Markdown text={run.prompt}/>{!!run.attachments?.length && <p className="wb-muted">附件：{attachmentNames(run.attachments)}</p>}</article>}
        {active && <article className="wb-message assistant" aria-live="polite"><CairnMark size={22}/><div>
          {stream.events.length > 0 && <details className="wb-process" open={!stream.preview}><summary>正在处理 · {stream.events.length} 条活动</summary><Timeline events={stream.events} compact/></details>}
          {stream.preview ? <Markdown text={stream.preview}/> : <p className="wb-muted">{run?.status === "queued" ? "等待 Server 执行…" : "Cairn 正在处理…"}</p>}
        </div></article>}
        {!active && run && <SettledTrace key={run.run_id} id={run.run_id}/>}
        {!active && !!run?.attachments?.length && <p className="wb-muted">本轮附件：{attachmentNames(run.attachments)}</p>}
        {!active && run?.error && <p className="wb-error">{run.error}{failed && <> · <a href={href("approvals", { run: run.run_id })}>打开恢复核对</a></>}</p>}
        {run?.task_id && <a className="wb-inline-link" href={href("tasks", { task: run.task_id })}>打开开发任务 <Icon name="arrow-right" size={14}/></a>}
        <div ref={tail}/>
      </div>
    </div>
    <div className="wb-chat-compose">{form}</div>
    {artifacts && <Drawer title="对话产物" close={() => setArtifacts(false)}><Outputs result={run?.result} history={history.data ?? []}/></Drawer>}
  </section>;
}
