import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { taskStatus } from "../runtime-api";
import { dateLabel, type ExecutionEvent, type Message } from "./data";
import { Icon } from "./Icon";

export function Markdown({ text }: { text: string }) {
  return <div className="wb-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
    img: ({ alt }) => <span>{alt || "图片"}</span>,
  }}>{text}</ReactMarkdown></div>;
}

export function Badge({ status }: { status: string }) {
  return <span className="status-badge" data-status={status}><i/>{taskStatus(status)}</span>;
}

const eventLabel: Record<string, string> = {
  accepted: "接收任务", running: "开始运行", worktree_ready: "准备工作树", conversation_ready: "建立执行会话",
  tool_start: "调用工具", tool_end: "工具结束", tool_result: "工具结果", completed: "运行完成",
  failed: "运行失败", paused: "已暂停", cancelled: "已取消", recovery_required: "需要核对",
  interrupted: "执行中断", reconciled: "人工核对", agent_step: "Agent 步骤", trace_finish: "执行指标",
};

export const activityEvents = (events: ExecutionEvent[]) => events.filter(event => !["model_text_delta", "model_reasoning_delta", "model_stream_start", "model_stream_end"].includes(event.kind));

export function Timeline({ events, compact = false }: { events: ExecutionEvent[]; compact?: boolean }) {
  const visible = activityEvents(events);
  if (!visible.length) return <p className="wb-muted">暂无已记录的活动。</p>;
  return <ol className={`wb-timeline ${compact ? "compact" : ""}`}>{visible.map(event => {
    const tool = event.data.tool ?? event.data.tool_name ?? event.data.name;
    return <li key={event.sequence}><details>
      <summary><Icon name={event.kind.startsWith("tool") ? "terminal" : "clock"} size={14}/><span>{eventLabel[event.kind] ?? event.kind}{typeof tool === "string" ? " · " + tool : ""}</span>{event.created_at && !compact && <time>{dateLabel(event.created_at)}</time>}</summary>
      {Object.keys(event.data).length > 0 && <pre>{JSON.stringify(event.data, null, 2)}</pre>}
    </details></li>;
  })}</ol>;
}

export function Outputs({ result, history }: { result?: string | null; history: Message[] }) {
  const [query, setQuery] = useState("");
  const tools = new Map(history.flatMap(message => message.tool_calls ?? []).map(call => [call.id, call.name]));
  const outputs = history.flatMap((message, index) => message.role === "tool" && message.content ? [{ id: index, title: tools.get(message.tool_call_id ?? "") ?? "工具输出", text: message.content }] : []);
  const artifacts = [...(result ? [{ id: -1, title: "执行结果", text: result }] : []), ...outputs];
  const matches = artifacts.filter(item => item.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()) || item.text.toLocaleLowerCase().includes(query.toLocaleLowerCase()));

  return <div className="wb-outputs">
    {!!artifacts.length && <input type="search" aria-label="搜索产物" placeholder="搜索产物" value={query} onChange={event => setQuery(event.target.value)} />}
    {matches.map(item => <details className="wb-output" key={item.id} open={item.id === -1}>
      <summary><Icon name={item.id === -1 ? "doc" : "terminal"} size={15}/>{item.title}</summary>
      {item.id === -1 ? <Markdown text={item.text}/> : <pre>{item.text}</pre>}
    </details>)}
    {!matches.length && <p className="wb-muted">{query ? "没有匹配的产物。" : "暂无已提交的运行结果或工具输出。"}</p>}
    <p className="wb-muted">文件与 Diff 预览需要 Server 提供受限的产物读取接口。</p>
  </div>;
}

export function Drawer({ title, close, children }: { title: string; close: () => void; children: React.ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog ref={dialog} className="wb-drawer" aria-label={title} onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <div className="wb-drawer-inner">
      <header><h2>{title}</h2><button aria-label="关闭抽屉" onClick={close}><Icon name="close" size={16}/></button></header>
      {children}
    </div>
  </dialog>;
}
