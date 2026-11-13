import { useCallback, useEffect, useRef, useState } from "react";
import { newRequestId } from "./ids";
import { PageHeader } from "./workbench/execution";
import { runtimeApi as api } from "./runtime-api";

type Section = "wechat" | "schedule" | "memory";
type Wx = { status: string; running: boolean; login_pending: boolean; verification_required: boolean; qr_image: string | null; account_saved: boolean; enabled: boolean; trusted_senders: string[] };
type Job = { job_id: string; prompt: string; kind: string; next_run_at: string | null; enabled: boolean; reminder: boolean };
type Memory = { enabled: boolean; agent_md: string; memory_md: string; daily_days: number; pending: { text: string; reason: string } | null; proposal_digest: string | null };
type Snapshot = { workspace: string; notices: string[]; pending_operations: { id: string; command: string }[]; wechat?: Wx; schedule?: { jobs: Job[]; runs: Record<string, unknown>[]; owner_available: boolean }; memory?: Memory };
export function SettingsPage() {
  const [section, setSection] = useState<Section>(() => {
    const requested = new URLSearchParams(location.search).get("section");
    return requested === "memory" || requested === "schedule" ? requested : "wechat";
  });
  const [data, setData] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [feedback, setFeedback] = useState("");
  const [code, setCode] = useState(""), [sender, setSender] = useState("");
  const [kind, setKind] = useState("once"), [when, setWhen] = useState(""), [goal, setGoal] = useState(""), [reminder, setReminder] = useState(true);
  const [note, setNote] = useState(""), [proposal, setProposal] = useState(""), [approved, setApproved] = useState(false);
  const [disconnect, setDisconnect] = useState(false), [ack, setAck] = useState(false);
  const requests = useRef(new Map<string, { command: string; args: string; source: string; request_id: string; confirmed: boolean; expected_proposal: string | null }>());
  const current = useRef(section); current.current = section;
  const refresh = useCallback(async () => {
    const target = section;
    try { const value = await api(`management?section=${target}`); if (current.current === target) setData(value); }
    catch (reason) { if (current.current === target) setError(String(reason)); }
  }, [section]);
  useEffect(() => { setData(null); setError(""); setFeedback(""); setApproved(false); void refresh(); }, [refresh]);
  useEffect(() => {
    if (section !== "wechat") return;
    const timer = setInterval(() => void refresh(), 2000); return () => clearInterval(timer);
  }, [section, refresh]);
  const stamp = data?.memory?.proposal_digest;
  useEffect(() => setApproved(false), [stamp]);
  async function command(name: string, args: string, confirmed = false, expected: string | null = null) {
    if (busy) return false;
    setBusy(true); setError(""); setFeedback("");
    const signature = JSON.stringify([name, args, confirmed, expected]);
    if (!requests.current.has(signature)) requests.current.set(signature, { command: name, args, source: "web", request_id: newRequestId(), confirmed, expected_proposal: expected });
    try {
      const value = await api("commands", requests.current.get(signature)); requests.current.delete(signature); setFeedback(value.message); await refresh(); return value.ok === true;
    } catch (reason) { setError(String(reason)); return false; }
    finally { setBusy(false); }
  }
  async function addSchedule() {
    let time = when;
    if (kind === "once") { const date = new Date(when); if (!Number.isFinite(date.getTime())) { setError("请选择有效的执行时间"); return; } time = date.toISOString(); }
    if (await command("/schedule", `${reminder ? "remind" : "add"} ${kind} ${time} ${goal}`)) setGoal("");
  }
  const wx = data?.wechat, schedules = data?.schedule, memory = data?.memory;
  return <div className="wb-page settings-main">
    <PageHeader icon="settings" title="设置" description="管理微信连接、定时任务与个人记忆" actions={<button disabled={busy} onClick={() => void refresh()}>刷新</button>}/>
    <nav className="settings-tabs" aria-label="管理分类">{([ ["wechat", "微信"], ["schedule", "定时任务"], ["memory", "记忆"] ] as const).map(([key, label]) => <button key={key} aria-current={section === key ? "page" : undefined} disabled={busy} onClick={() => setSection(key)}>{label}</button>)}</nav>
    {error && <p role="alert" className="control-error">{error}。操作结果不明时先刷新核对, 重试会沿用原请求编号。</p>}
    {feedback && <pre className="control-feedback" role="status">{feedback}</pre>}
    {!data && !error && <p>正在读取 Server 状态…</p>}
    {wx && <section className="settings-section"><h2>微信连接</h2><pre className="connection-status">{wx.status}</pre><div className="control-row"><button className="primary" disabled={busy || wx.running || wx.login_pending} onClick={() => void command("/wx", "")}>{wx.account_saved ? "启动已登录账号" : "登录微信"}</button><button disabled={busy || wx.running || wx.login_pending} onClick={() => void command("/wx", "login")}>重新扫码登录</button><button disabled={busy || (!wx.running && !wx.login_pending)} onClick={() => void command("/wx", "stop")}>停止接收</button></div>
      {wx.login_pending && <p role="status">等待扫码。关闭网页或 CLI 后, 登录流程仍由 Server 继续。</p>}
      {wx.qr_image && <figure className="wechat-qr"><img src={wx.qr_image} width="240" height="240" alt="微信登录二维码"/><figcaption>使用微信扫码登录</figcaption></figure>}
      {wx.verification_required && <form onSubmit={event => { event.preventDefault(); void command("/wx", `verify ${code}`).then(ok => { if (ok) setCode(""); }); }}><label>登录验证码<input autoComplete="one-time-code" value={code} onChange={event => setCode(event.target.value)} maxLength={50}/></label><button disabled={busy || !code.trim()}>提交验证码</button></form>}
      <h3>允许接入项目的微信用户</h3><p className="muted">绑定用户可使用个人记忆和自己的计划。额外用户只获得项目访问。</p><ul>{wx.trusted_senders.map(id => <li key={id}>{id}</li>)}</ul><form className="control-row" onSubmit={event => { event.preventDefault(); void command("/wx", `allow ${sender}`).then(ok => { if (ok) setSender(""); }); }}><label>用户 ID<input value={sender} onChange={event => setSender(event.target.value)} maxLength={200}/></label><button disabled={busy || !sender.trim()}>允许接入</button></form>
      <details className="danger-controls"><summary>移除登录</summary><label><input type="checkbox" checked={disconnect} onChange={event => setDisconnect(event.target.checked)}/>确认停止微信并移除保存的账号</label><button disabled={busy || !disconnect} onClick={() => void command("/wx", "disconnect").then(ok => { if (ok) setDisconnect(false); })}>断开并移除账号</button></details>
    </section>}
    {schedules && <section className="settings-section"><h2>定时任务</h2><p className="muted">纯提醒独立发送。执行任务进入共享队列; 离线期间未执行的计划恢复后补跑, 不重放结果不明的操作。</p>{!schedules.owner_available && <p>先在「微信」登录并绑定提醒接收人。</p>}
      <form onSubmit={event => { event.preventDefault(); void addSchedule(); }} className="schedule-form"><label>类型<select value={reminder ? "remind" : "execute"} onChange={event => setReminder(event.target.value === "remind")}><option value="remind">纯提醒</option><option value="execute">执行 Agent 任务后通知</option></select></label><label>频率<select value={kind} onChange={event => { setKind(event.target.value); setWhen(""); }}><option value="once">一次性</option><option value="daily">每天</option><option value="interval">间隔秒数</option></select></label><label>{kind === "daily" ? "Asia/Shanghai 时间" : kind === "interval" ? "间隔秒数" : "执行时间 (浏览器本地时间)"}<input required type={kind === "once" ? "datetime-local" : kind === "daily" ? "time" : "number"} value={when} min={kind === "interval" ? 60 : undefined} onChange={event => setWhen(event.target.value)}/></label><label className="full-width">{reminder ? "提醒内容" : "任务目标"}<textarea required value={goal} onChange={event => setGoal(event.target.value)} maxLength={12000} rows={3}/></label><button className="primary" disabled={busy || !schedules.owner_available || !goal.trim() || !when}>创建计划</button></form>
      <div className="schedule-list">{schedules.jobs.map(job => <article key={job.job_id}><div><h3>{job.prompt}</h3><p className="muted">{job.job_id} · {job.reminder ? "提醒" : "执行"} · {job.enabled ? "启用" : "暂停"} · {job.next_run_at ? new Date(job.next_run_at).toLocaleString() : "已结束"}</p></div><div className="control-row"><button disabled={busy} onClick={() => void command("/schedule", `${job.enabled ? "pause" : "resume"} ${job.job_id}`)}>{job.enabled ? "暂停" : "恢复"}</button><button disabled={busy} onClick={() => void command("/schedule", `remove ${job.job_id}`)}>移除</button></div></article>)}{!schedules.jobs.length && <p className="muted">暂无计划</p>}</div>
      <details><summary>最近执行记录</summary><pre>{JSON.stringify(schedules.runs, null, 2)}</pre></details>
    </section>}
    {memory && <section className="settings-section"><h2>个人记忆</h2><p className="muted">{memory.enabled ? "已启用" : "宿主已禁用"} · 已记录 {memory.daily_days} 天。任务结果自动记录, 行为规则需确认后生效。</p><form onSubmit={event => { event.preventDefault(); void command("/memory", `note ${note}`).then(ok => { if (ok) setNote(""); }); }}><label>记录偏好或经验<textarea value={note} onChange={event => setNote(event.target.value)} maxLength={12000} rows={3}/></label><button disabled={busy || !note.trim()}>保存记忆</button></form><div className="control-row"><button disabled={busy} onClick={() => void command("/memory", "distill")}>提炼近期经验</button><button disabled={busy} onClick={() => void command("/memory", "propose-auto")}>根据经验生成规则候选</button></div>
      <details><summary>当前 AGENT.md</summary><pre>{memory.agent_md}</pre></details><details><summary>长期记忆 MEMORY.md</summary><pre>{memory.memory_md}</pre></details>
      <form onSubmit={event => { event.preventDefault(); void command("/memory", `propose ${proposal}`).then(ok => { if (ok) setProposal(""); }); }}><label>提出行为规则变更<textarea value={proposal} onChange={event => setProposal(event.target.value)} maxLength={12000} rows={3}/></label><button disabled={busy || !proposal.trim()}>生成候选</button></form>
      {memory.pending && <section className="proposal"><h3>待审批候选</h3><p>{memory.pending.reason}</p><pre>{memory.pending.text}</pre><label><input type="checkbox" checked={approved} onChange={event => setApproved(event.target.checked)}/>已审阅上方候选, 确认应用这份规则</label><div className="control-row"><button className="primary" disabled={busy || !approved} onClick={() => void command("/memory", "approve", true, memory.proposal_digest)}>应用候选</button><button disabled={busy} onClick={() => void command("/memory", "reject")}>放弃候选</button></div></section>}
      <button disabled={busy} onClick={() => void command("/memory", "rollback")}>回滚最近一次规则变更</button>
    </section>}
    {!!data?.pending_operations.length && <section className="settings-section"><h2>需要人工核对的管理操作</h2><p>先查看实际结果, 确认后只解除等待, 不会重放。</p><label><input type="checkbox" checked={ack} onChange={event => setAck(event.target.checked)}/>已核对对应操作结果</label>{data.pending_operations.map(operation => <div className="control-row" key={operation.id}><code>{operation.command} · {operation.id}</code><button disabled={busy || !ack} onClick={() => void command("/management", `acknowledge ${operation.id}`, true)}>记录核对结果</button></div>)}</section>}
  </div>;
}
