import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { runtimeApi, taskStatus } from "./runtime-api";

type Conversation = {
  conversation_id: string;
  source: string;
  initial_prompt: string;
  created_at: string;
  updated_at: string;
  latest_run?: Run | null;
};

type Run = {
  run_id: string;
  conversation_id: string;
  status: string;
  task_id: string | null;
  cursor: number;
  result: string | null;
  error: string | null;
};

type Message = { role: string; content?: string | null };

function MarkdownBody({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: props => (
            <a {...props} target="_blank" rel="noopener noreferrer" />
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function short(value: string): string {
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-5)}` : value;
}

export function ConversationPanel() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState(new URLSearchParams(location.search).get("conversation") || "");
  const [messages, setMessages] = useState<Message[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [preview, setPreview] = useState("");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef<{ prompt: string; requestId: string; conversationRequestId: string } | null>(null);

  useEffect(() => {
    const saved = sessionStorage.getItem("cairn.pending");
    if (!saved) return;
    try {
      const value = JSON.parse(saved);
      if (value.prompt && value.requestId && value.conversationRequestId) {
        pending.current = value;
        setPrompt(value.prompt);
      }
    } catch {
      sessionStorage.removeItem("cairn.pending");
    }
  }, []);

  const selected = conversations.find(item => item.conversation_id === active);

  useEffect(() => {
    let live = true;
    const refresh = async () => {
      try {
        const rows: Conversation[] = await runtimeApi("conversations");
        if (live) setConversations(rows);
      } catch (reason) {
        if (live) setError(String(reason));
      }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => { live = false; clearInterval(timer); };
  }, []);

  useEffect(() => {
    const url = new URL(location.href);
    if (active) url.searchParams.set("conversation", active);
    else url.searchParams.delete("conversation");
    window.history.replaceState(null, "", url);
    setMessages([]);
    setRun(null);
    if (!active) return;
    let live = true;
    const load = async () => {
      try {
        const detail = await runtimeApi(`conversations/${active}`);
        const history: Message[] = await runtimeApi(`conversations/${active}/history`);
        if (!live) return;
        setMessages(history);
        const latest = detail.runs?.[detail.runs.length - 1] ?? null;
        setRun(latest);
      } catch (reason) {
        if (live) setError(String(reason));
      }
    };
    void load();
    return () => { live = false; };
  }, [active]);

  useEffect(() => {
    if (!run || !["queued", "running"].includes(run.status)) return;
    const timer = setInterval(async () => {
      try {
        const current: Run = await runtimeApi(`runs/${run.run_id}`);
        setRun(current);
        if (current.status === "queued" || current.status === "running") return;
        const history: Message[] = await runtimeApi(`conversations/${run.conversation_id}/history`);
        setMessages(history);
      } catch (reason) {
        setError(String(reason));
      }
    }, 500);
    return () => clearInterval(timer);
  }, [run]);

  useEffect(() => {
    setPreview("");
  }, [run?.run_id]);

  useEffect(() => {
    if (!run || !["queued", "running"].includes(run.status)) {
      setPreview("");
      return;
    }
    let live = true;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let socket: WebSocket | undefined;
    const cursor = { current: 0 };
    const connect = () => {
      if (!live) return;
      socket = new WebSocket(
        `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/runtime/runs/${run.run_id}/stream?after=${cursor.current}`,
      );
      socket.onmessage = message => {
        if (!live) return;
        const data = JSON.parse(message.data);
        if (data.type === "ready") {
          cursor.current = Math.max(cursor.current, Number(data.cursor) || 0);
        } else if (data.type === "event") {
          const event = data.event;
          if (!event || Number(event.sequence) <= cursor.current) return;
          cursor.current = Number(event.sequence);
          if (event.kind === "model_text_delta" && event.data?.text) {
            setPreview(old => (old + String(event.data.text)).slice(-64000));
          } else if (["running", "failed", "paused", "completed", "recovery_required", "cancelled"].includes(event.kind)) {
            setPreview("");
          }
        } else if (data.type === "preview") {
          setPreview(old => (old + data.text).slice(-64000));
        } else if (data.type === "preview_snapshot") {
          setPreview(String(data.text).slice(-64000));
        } else if (data.type === "settled") {
          cursor.current = Math.max(cursor.current, Number(data.cursor) || 0);
          live = false;
          socket?.close();
        }
      };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        if (live) reconnectTimer = setTimeout(connect, 500);
      };
    };
    connect();
    return () => {
      live = false;
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [run?.run_id, run?.status]);

  async function send() {
    const text = prompt.trim();
    if (!text || busy) return;
    if (pending.current && pending.current.prompt !== text) {
      setError("上一次发送结果尚未确认，请保持原内容重试。");
      return;
    }
    setBusy(true);
    setError("");
    const requestId = pending.current?.requestId ?? `web:${crypto.randomUUID()}`;
    const conversationRequestId = pending.current?.conversationRequestId ?? `web-conv:${crypto.randomUUID()}`;
    pending.current = { prompt: text, requestId, conversationRequestId };
    sessionStorage.setItem("cairn.pending", JSON.stringify(pending.current));
    try {
      let conversationId = active;
      if (!conversationId) {
        const created: Conversation = await runtimeApi("conversations", {
          prompt: text,
          source: "web",
          request_id: conversationRequestId,
        });
        conversationId = created.conversation_id;
        setActive(conversationId);
      }
      const next: Run = await runtimeApi(`conversations/${conversationId}/messages`, {
        prompt: text,
        source: "web",
        request_id: requestId,
      });
      setRun(next);
      setPrompt("");
      pending.current = null;
      sessionStorage.removeItem("cairn.pending");
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat-layout">
      <aside className="conversations">
        <button className="secondary" onClick={() => { setActive(""); setMessages([]); setRun(null); }}>
          新对话
        </button>
        {conversations.map(item => (
          <button
            key={item.conversation_id}
            className={`conversation${item.conversation_id === active ? " selected" : ""}`}
            onClick={() => setActive(item.conversation_id)}
          >
            <span>{item.initial_prompt || short(item.conversation_id)}</span>
            <small>{item.latest_run?.status ?? "未执行"}</small>
          </button>
        ))}
      </aside>
      <section className="chat-thread">
        <header className="chat-header">
          <strong>{selected ? short(selected.conversation_id) : "新对话"}</strong>
          {run ? <span>{taskStatus(run.status)}</span> : null}
        </header>
        <div className="messages">
          {messages.map((message, index) => (
            message.content ? (
              <div key={index} className={`message message--${message.role}`}>
                <small>{message.role}</small>
                <MarkdownBody content={message.content} />
              </div>
            ) : null
          ))}
          {preview ? <div className="message message--assistant"><small>assistant</small><MarkdownBody content={preview} /></div> : null}
          {run?.error ? <div className="control-error">{run.error}</div> : null}
        </div>
        {error ? <div className="control-error">{error}</div> : null}
        <form className="chat-composer" onSubmit={event => { event.preventDefault(); void send(); }}>
          <textarea
            value={prompt}
            onChange={event => setPrompt(event.target.value)}
            placeholder="输入消息…"
            rows={3}
          />
          <button disabled={busy || !prompt.trim()} type="submit">{busy ? "处理中" : "发送"}</button>
        </form>
      </section>
    </div>
  );
}
