import { useEffect, useState } from "react";
import { RuntimeError, runtimeApi } from "../runtime-api";
import { isActive, type Conversation, type ExecutionEvent, type Run, type Task } from "./data";

export type Submission = {
  prompt: string;
  id: string;
  conversation: string;
  mode: "conversation" | "development";
  model?: string;
};

export function readSubmission(key: string): Submission | null {
  try {
    const item = JSON.parse(sessionStorage.getItem(key) ?? "null");
    return item && typeof item.id === "string" && typeof item.prompt === "string" && typeof item.conversation === "string" &&
      (item.mode === "conversation" || item.mode === "development") ? item : null;
  } catch {
    return null;
  }
}

export async function sendSubmission(submission: Submission, key: string) {
  // A record left in storage means this is a retry after a lost response.
  // Query the durable request identity before posting again.
  const resuming = sessionStorage.getItem(key) !== null;
  sessionStorage.setItem(key, JSON.stringify(submission));
  if (submission.mode === "development") {
    const created = await runtimeApi<{ task: Task }>("developments", {
      prompt: submission.prompt,
      source: "web",
      request_id: submission.id,
      ...(submission.conversation ? { conversation_id: submission.conversation } : {}),
    });
    sessionStorage.removeItem(key);
    return { task: created.task };
  }
  if (!submission.conversation) {
    const created = await runtimeApi<Conversation>("conversations", {
      prompt: submission.prompt, source: "web", request_id: "create:" + submission.id,
    });
    submission.conversation = created.conversation_id;
    sessionStorage.setItem(key, JSON.stringify(submission));
  }
  if (resuming) {
    try {
      const existing = await runtimeApi<Run>(
        `conversations/${encodeURIComponent(submission.conversation)}/runs/by-request/${encodeURIComponent(submission.id)}`
      );
      sessionStorage.removeItem(key);
      return { run: existing };
    } catch (reason) {
      if (!(reason instanceof RuntimeError && reason.status === 404)) throw reason;
    }
  }
  const run = await runtimeApi<Run>(`conversations/${encodeURIComponent(submission.conversation)}/messages`, {
    prompt: submission.prompt, source: "web", request_id: submission.id,
    ...(submission.model ? { model_id: submission.model } : {}),
  });
  sessionStorage.removeItem(key);
  return { run };
}

export function useRunStream(run?: Run) {
  const id = run?.run_id;
  const live = Boolean(run && isActive(run.status));
  const [state, setState] = useState<{ id?: string; preview: string; events: ExecutionEvent[] }>({ preview: "", events: [] });

  useEffect(() => {
    if (!id || !live) return;
    let active = true;
    let cursor = 0;
    let socket: WebSocket;
    let timer: ReturnType<typeof setTimeout>;
    let settled = false;
    const events: ExecutionEvent[] = [];
    let preview = "";

    function connect() {
      const url = new URL(`/api/runtime/runs/${encodeURIComponent(id!)}/stream`, location.href);
      url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
      url.searchParams.set("after", String(cursor));
      socket = new WebSocket(url);
      socket.onmessage = message => {
        if (!active) return;
        const data = JSON.parse(message.data) as { type: string; text?: string; event?: ExecutionEvent };
        if (data.type === "settled") settled = true;
        else if (data.type === "preview_snapshot") preview = data.text ?? "";
        else if (data.type === "preview") preview = (preview + (data.text ?? "")).slice(-65536);
        else if (data.type === "event" && data.event && data.event.sequence > cursor) {
          cursor = data.event.sequence;
          if (!data.event.kind.startsWith("model_")) events.push(data.event);
        }
        setState({ id, preview, events: events.slice(-200) });
      };
      socket.onclose = () => {
        if (active && !settled) timer = setTimeout(connect, 750);
      };
    }
    setState({ id, preview: "", events: [] });
    connect();
    return () => { active = false; clearTimeout(timer); socket.close(); };
  }, [id, live]);

  return state.id === id ? state : { preview: "", events: [] };
}
