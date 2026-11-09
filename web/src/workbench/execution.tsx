import { useEffect, useState, type ReactNode } from "react";
import { runtimeApi } from "../runtime-api";
import { dateLabel, isActive, sourceLabel, useResource, type ExecutionEvent, type Message, type Run, type Task } from "./data";
import { activityEvents, Badge, Drawer, Markdown, Outputs, Timeline } from "./evidence";
import { Icon, type IconName } from "./Icon";
import { href } from "./Shell";

export function PageHeader({ icon, title, description, actions }: { icon: IconName; title: string; description?: string; actions?: ReactNode }) {
  return <header className="wb-page-head"><span className="wb-page-icon"><Icon name={icon} size={20}/></span><div><h1>{title}</h1>{description && <p>{description}</p>}</div>{actions && <div className="wb-actions">{actions}</div>}</header>;
}

export function ErrorText({ value }: { value?: string }) {
  return value ? <p role="alert" className="wb-error">{value}</p> : null;
}

export function useEventLog(resource: string, active: boolean): { rows: ExecutionEvent[]; error?: string } {
  const [entry, setEntry] = useState<{ resource: string; rows: ExecutionEvent[]; error?: string }>();
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let cursor = 0;
    let rows: ExecutionEvent[] = [];

    async function read() {
      try {
        while (!controller.signal.aborted) {
          const page = await runtimeApi<{ events: ExecutionEvent[]; cursor: number }>(`${resource}/events?after=${cursor}`, undefined, controller.signal);
          if (controller.signal.aborted) return;
          const next = activityEvents(page.events.filter(event => event.sequence > cursor));
          rows = [...rows, ...next].slice(-400);
          setEntry({ resource, rows });
          if (page.cursor <= cursor) break;
          cursor = page.cursor;
        }
      } catch (reason) {
        if (!controller.signal.aborted) setEntry({ resource, rows, error: String(reason) });
      } finally {
        if (active && !controller.signal.aborted) timer = setTimeout(read, 1500);
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [resource, active]);
  return entry?.resource === resource ? entry : { rows: [] };
}

function TaskCard({ task, navigate }: { task: Task; navigate: (path: string) => void }) {
  const url = href("tasks", { task: task.task_id });
  return <a className="task-card" href={url} onClick={event => { if (!event.metaKey && !event.ctrlKey) { event.preventDefault(); navigate(url); } }}>
    <div className="task-card-top"><span className="task-symbol"><Icon name="board" size={17}/></span><Badge status={task.status}/></div>
    <h2>{task.prompt}</h2>
    <div className="wb-task-meta"><span><Icon name="branch" size={14}/>{task.branch ?? "等待工作树"}</span><span>{task.attempts} 次执行</span></div>
    {task.diagnostic && <p className="card-diagnostic">{task.diagnostic}</p>}
    <footer><small>{sourceLabel(task.source)}</small><code>{task.task_id.slice(0, 17)}</code><Icon name="arrow-right" size={15}/></footer>
  </a>;
}

export function Tasks({ tasks, id, navigate, refresh, loading, error }: {
  tasks: Task[]; id: string; navigate: (path: string) => void; refresh: () => void; loading: boolean; error?: string;
}) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  if (id) return <TaskDetail key={id} id={id} navigate={navigate} refresh={refresh}/>;
  const matches = tasks.filter(task => (filter === "all" || (filter === "active" ? isActive(task.status) : filter === "review" ? task.status === "recovery_required" : task.status === filter)) && (task.prompt + " " + task.task_id).toLowerCase().includes(search.toLowerCase()));
  return <div className="wb-page">
    <PageHeader icon="board" title="开发任务" description="共享工作区中的开发目标和执行进度" actions={<button className="primary" onClick={() => navigate(href("home", { mode: "development" }))}><Icon name="plus" size={15}/> 新建任务</button>}/>
    <div className="wb-toolbar">
      <label><Icon name="search" size={15}/><input type="search" aria-label="搜索任务" placeholder="搜索任务" value={search} onChange={event => setSearch(event.target.value)}/></label>
      <select aria-label="筛选任务状态" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部状态</option><option value="active">运行中</option><option value="review">待核对</option><option value="completed">已完成</option><option value="failed">失败</option></select>
    </div>
    <ErrorText value={error}/>
    <div className="task-grid">{matches.map(task => <TaskCard key={task.task_id} task={task} navigate={navigate}/>)}</div>
    {!matches.length && !error && <Empty icon="board" text={loading ? "正在读取开发任务…" : "暂无匹配的开发任务。"}/>}
  </div>;
}

function TaskFollowup({ task, resource, onSent }: { task: Task; resource: string; onSent: () => void }) {
  const key = "cairn.web.pending.task:" + task.task_id;
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<{ prompt: string; request_id: string; expected_sequence: number } | null>(() => {
    try { return JSON.parse(sessionStorage.getItem(key) ?? "null"); } catch { return null; }
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const acceptsInput = task.status === "paused" || task.status === "completed";

  async function submit() {
    if (busy || (!pending && (!acceptsInput || !draft.trim()))) return;
    const input = pending ?? { prompt: draft.trim(), request_id: crypto.randomUUID(), expected_sequence: task.sequence };
    setBusy(true);
    setError("");
    try {
      sessionStorage.setItem(key, JSON.stringify(input));
      setPending(input);
      await runtimeApi(resource + "/messages", { ...input, source: "web" });
      sessionStorage.removeItem(key);
      setPending(null);
      setDraft("");
      onSent();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!acceptsInput && !pending) return null;
  return <section className="wb-card wb-followup">
    <h2>跟进开发任务</h2><ErrorText value={error}/>
    <form onSubmit={event => { event.preventDefault(); void submit(); }}>
      <textarea aria-label="跟进说明" placeholder="补充要求或继续当前工作树…" maxLength={32000} value={pending?.prompt ?? draft} onChange={event => setDraft(event.target.value)} disabled={busy || !!pending}/>
      <div className="wb-link-row">
        <button className="primary" disabled={busy || (!pending && !draft.trim())}>{pending ? "用原编号重试" : "发送跟进"}</button>
        {pending && <button type="button" disabled={busy} onClick={() => { sessionStorage.removeItem(key); setDraft(pending.prompt); setPending(null); setError(""); onSent(); }}>清除本地记录</button>}
      </div>
    </form>
  </section>;
}

function TaskDetail({ id, navigate, refresh }: { id: string; navigate: (path: string) => void; refresh: () => void }) {
  const resource = `tasks/${encodeURIComponent(id)}`;
  const info = useResource<Task>(resource, 1300);
  const task = info.data;
  const events = useEventLog(resource, !!task && isActive(task.status));
  const history = useResource<Message[]>(task && !isActive(task.status) ? resource + "/history" : null);
  const [outputs, setOutputs] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function act(action: "cancel" | "continue") {
    if (busy) return;
    setBusy(true);
    setError("");
    try { await runtimeApi(resource + "/" + action, {}); info.refresh(); refresh(); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  return <div className="wb-page">
    <button className="wb-back" onClick={() => navigate(href("tasks"))}>‹ 返回开发任务</button>
    <PageHeader icon="board" title={task?.prompt ?? "开发任务"} description={id} actions={task && <>
      <Badge status={task.status}/>
      <button onClick={() => setOutputs(true)}><Icon name="artifact" size={16}/> 产物</button>
      {isActive(task.status) && <button disabled={busy} onClick={() => void act("cancel")}>中断任务</button>}
      {task.status === "paused" && <button disabled={busy} onClick={() => void act("continue")}>安全继续</button>}
    </>}/>
    <ErrorText value={error || info.error}/>
    {task && <>
      <div className="wb-detail-grid">
        <section className="wb-card"><h2>工作树</h2><dl><dt>分支</dt><dd>{task.branch ?? "等待分配"}</dd><dt>路径</dt><dd>{task.worktree ?? "等待分配"}</dd><dt>Session</dt><dd>{task.session_id ?? "—"}</dd></dl></section>
        <section className="wb-card"><h2>最近执行</h2><dl><dt>执行次数</dt><dd>{task.attempts}</dd><dt>来源</dt><dd>{sourceLabel(task.source)}</dd><dt>步骤</dt><dd>{typeof task.metrics.steps === "number" ? task.metrics.steps : "—"}</dd></dl></section>
      </div>
      {task.diagnostic && <p className="wb-error" role="status">{task.diagnostic}</p>}
      {task.status === "recovery_required" && <a className="wb-inline-link" href={href("approvals", { task: id })}>打开恢复核对 <Icon name="arrow-right" size={14}/></a>}
      <section className="wb-card"><h2>执行时间线</h2><ErrorText value={events.error}/><Timeline events={events.rows}/><button onClick={() => navigate(href("workflow", { task: id }))}>查看流程图</button></section>
      {task.result && <section className="wb-card"><h2>任务结果</h2><Markdown text={task.result}/></section>}
      <section className="wb-card"><h2>验证与依赖</h2><p className="wb-muted">已记录的验证输出见产物与执行时间线。独立测试结论和任务依赖等待 Workflow 契约。</p></section>
      <TaskFollowup task={task} resource={resource} onSent={() => { info.refresh(); refresh(); }}/>
      {outputs && <Drawer title="任务产物" close={() => setOutputs(false)}><ErrorText value={history.error}/><Outputs result={task.result} history={history.data ?? []}/></Drawer>}
    </>}
  </div>;
}

export function Runs({ runs, id, navigate, error }: { runs: Run[]; id: string; navigate: (path: string) => void; error?: string }) {
  const [filter, setFilter] = useState("");
  if (id) return <RunDetail key={id} id={id} navigate={navigate}/>;
  const matches = runs.filter(run => `${run.prompt ?? ""} ${run.run_id} ${sourceLabel(run.source)}`.toLowerCase().includes(filter.toLowerCase()));
  return <div className="wb-page">
    <PageHeader icon="runs" title="运行" description="当前工作区最近 100 次共享执行"/>
    <div className="wb-toolbar"><label><Icon name="search" size={15}/><input type="search" aria-label="搜索运行" placeholder="搜索运行或来源" value={filter} onChange={event => setFilter(event.target.value)}/></label></div>
    <ErrorText value={error}/>
    <div className="wb-run-list">{matches.map(run => <a key={run.run_id} href={href("runs", { run: run.run_id })} onClick={event => { if (!event.metaKey && !event.ctrlKey) { event.preventDefault(); navigate(event.currentTarget.getAttribute("href")!); } }}>
      <span className="task-symbol"><Icon name="runs" size={16}/></span>
      <div><strong>{run.prompt || run.run_id}</strong><small>{sourceLabel(run.source)} · {dateLabel(run.created_at)}</small></div>
      <Badge status={run.status}/><Icon name="arrow-right" size={15}/>
    </a>)}</div>
    {!matches.length && !error && <Empty icon="runs" text="暂无匹配的运行。"/>}
  </div>;
}

function RunDetail({ id, navigate }: { id: string; navigate: (path: string) => void }) {
  const resource = `runs/${encodeURIComponent(id)}`;
  const info = useResource<Run>(resource, 1400);
  const run = info.data;
  const events = useEventLog(resource, !!run && isActive(run.status));
  const [drawer, setDrawer] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function cancel() {
    if (busy) return;
    setBusy(true);
    try { await runtimeApi(resource + "/cancel", {}); info.refresh(); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  return <div className="wb-page">
    <button className="wb-back" onClick={() => navigate(href("runs"))}>‹ 返回运行列表</button>
    <PageHeader icon="runs" title={run?.prompt || "运行详情"} description={id} actions={run && <>
      <Badge status={run.status}/><button onClick={() => setDrawer(true)}>产物</button>
      {run.mode !== "development" && isActive(run.status) && <button disabled={busy} onClick={() => void cancel()}>中断运行</button>}
    </>}/>
    <ErrorText value={error || info.error || run?.error || undefined}/>
    {run && <>
      <div className="wb-link-row">
        <a className="wb-inline-link" href={href("chat", { conversation: run.conversation_id })}>继续对话 <Icon name="arrow-right" size={14}/></a>
        {run.task_id && <a className="wb-inline-link" href={href("tasks", { task: run.task_id })}>打开开发任务 <Icon name="arrow-right" size={14}/></a>}
        {run.mode !== "development" && run.status === "recovery_required" && <a className="wb-inline-link" href={href("approvals", { run: run.run_id })}>打开恢复核对 <Icon name="arrow-right" size={14}/></a>}
      </div>
      <section className="wb-card"><h2>执行时间线</h2><ErrorText value={events.error}/><Timeline events={events.rows}/></section>
      {run.result && <section className="wb-card"><h2>运行结果</h2><Markdown text={run.result}/></section>}
      {run.budget && Object.keys(run.budget).length > 0 && <section className="wb-card"><h2>执行指标</h2><pre>{JSON.stringify(run.budget, null, 2)}</pre></section>}
      {drawer && <Drawer title="运行产物" close={() => setDrawer(false)}><Outputs result={run.result} history={[]}/></Drawer>}
    </>}
  </div>;
}

export function Empty({ icon, text }: { icon: IconName; text: string }) {
  return <div className="wb-empty"><Icon name={icon} size={28}/><p>{text}</p></div>;
}
