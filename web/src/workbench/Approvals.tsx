import { useEffect, useState } from "react";
import { runtimeApi } from "../runtime-api";
import { type Run, type Task, useResource } from "./data";
import { Empty, ErrorText, PageHeader } from "./execution";
import { Icon } from "./Icon";
import { href } from "./Shell";

type Recovery = {
  revision?: Record<string, unknown> | null;
  action: "abandon_without_replay" | "acknowledge_checkpoint";
  expected_sequence?: number;
  checkpoint_outcome: string;
  unresolved_turn_id: string | null;
  worktree: string | null;
  fences: Record<string, unknown>[];
};

export function Approvals({ task, run, tasks, runs, refresh, navigate, error }: {
  task: string; run: string; tasks: Task[]; runs: Run[]; refresh: () => void; navigate: (path: string) => void; error?: string;
}) {
  const rows = [
    ...tasks.filter(item => item.status === "recovery_required").map(item => ({ task: item.task_id, text: item.prompt, id: item.task_id })),
    ...runs.filter(item => item.mode !== "development" && item.status === "recovery_required" && !tasks.some(task => task.task_id === item.task_id)).map(item => ({ run: item.run_id, text: item.prompt || item.run_id, id: item.run_id })),
  ];
  const path = task ? `tasks/${encodeURIComponent(task)}` : run ? `runs/${encodeURIComponent(run)}` : null;
  const returnUrl = task ? href("tasks", { task }) : href("runs", { run });
  return <div className="wb-page">
    <PageHeader icon="gate" title="待我确认" description="检查已持久化的执行证据，明确记录恢复决定。"/>
    <ErrorText value={error}/>
    {path ? <Review key={path} path={path} returnUrl={returnUrl} onApplied={() => { refresh(); navigate(returnUrl); }} /> :
      <>
        <div className="wb-run-list">{rows.map(row => <a key={row.id} href={href("approvals", "task" in row ? { task: row.task! } : { run: row.run! })}><span className="task-symbol"><Icon name="gate" size={15}/></span><div><strong>{row.text}</strong><small>{row.id}</small></div><Icon name="arrow-right" size={16}/></a>)}</div>
        {!rows.length && <Empty icon="gate" text="当前没有需要恢复核对的执行。"/>}
      </>}
    <section className="wb-card"><h2>工作流审批</h2><p className="wb-muted">Workflow 审批节点等待 Server 契约。现有个人记忆规则审批可在设置中完成。</p><a className="wb-inline-link" href={href("settings", { section: "memory" })}>打开记忆设置 <Icon name="arrow-right" size={14}/></a></section>
  </div>;
}

function Review({ path, returnUrl, onApplied }: { path: string; returnUrl: string; onApplied: () => void }) {
  const review = useResource<Recovery>(path + "/recovery");
  const data = review.data;
  const revision = JSON.stringify(data?.revision);
  const [confirmed, setConfirmed] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { setConfirmed(false); }, [revision]);
  async function submit() {
    if (busy || !data || !confirmed || !note.trim()) return;
    setBusy(true);
    setError("");
    try {
      await runtimeApi(path + "/reconcile", {
        revision: data.revision, action: data.action, reviewed_effects: true, note: note.trim(),
        ...(path.startsWith("tasks/") ? { expected_sequence: data.expected_sequence } : {}),
      });
      onApplied();
    } catch (reason) {
      setError(String(reason));
      setConfirmed(false);
      review.refresh();
    } finally { setBusy(false); }
  }
  return <section className="wb-card wb-review">
    <h2>恢复核对</h2><ErrorText value={error || review.error}/>
    {!data && !review.error && <p>正在读取 Server 证据…</p>}
    {data && <form onSubmit={event => { event.preventDefault(); void submit(); }}>
      <p>{data.action === "abandon_without_replay" ? "结束不确定的轮次，保留已记录的影响。" : "确认当前检查点与实际执行结果一致。"}</p>
      <dl><dt>检查点</dt><dd>{data.checkpoint_outcome}</dd><dt>工作树</dt><dd>{data.worktree ?? "共享工作区"}</dd><dt>待核对轮次</dt><dd>{data.unresolved_turn_id ?? "—"}</dd></dl>
      {data.fences.length > 0 && <details open><summary>工具副作用记录</summary><pre>{JSON.stringify(data.fences, null, 2)}</pre></details>}
      <label>核对说明<textarea maxLength={1024} required value={note} onChange={event => setNote(event.target.value)} placeholder="描述你核对到的实际影响"/></label>
      <label className="wb-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)}/>我已核对实际影响</label>
      <button className="primary" disabled={!confirmed || !note.trim() || busy}>记录核对决定</button>
    </form>}
    <a className="wb-inline-link" href={returnUrl}>返回执行详情</a>
  </section>;
}
