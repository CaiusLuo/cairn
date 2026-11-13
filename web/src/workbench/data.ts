import { useCallback, useEffect, useState } from "react";
import { runtimeApi } from "../runtime-api";

export type Run = {
  run_id: string;
  conversation_id: string;
  task_id: string | null;
  status: string;
  mode?: string;
  result: string | null;
  error: string | null;
  prompt?: string;
  source?: string;
  created_at?: string;
  ended_at?: string | null;
  budget?: Record<string, unknown>;
  cursor?: number;
  session_id?: string | null;
  metadata?: Record<string, unknown>;
};

export type Conversation = {
  conversation_id: string;
  source: string;
  initial_prompt: string;
  title?: string | null;
  updated_at: string;
  archived_at?: string | null;
  latest_run?: Run | null;
};

export const isActive = (status: string) => status === "queued" || status === "running";
export const sourceLabel = (source?: string) => ({ cli: "CLI", web: "Web", wechat: "微信", schedule: "定时任务" }[source ?? ""] ?? source ?? "");
export const dateLabel = (date?: string | null) => date ? new Date(date).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" }) : "—";
export function conversationTitle(prompt?: string | null, limit = 64) {
  const line = (prompt ?? "").split(/\r?\n/, 1)[0].replace(/\s+/g, " ").trim();
  if (!line) return "对话";
  return line.length > limit ? line.slice(0, limit).trimEnd() + "…" : line;
}

type Resource<T> = { data?: T; error?: string };

export function useResource<T>(path: string | null, interval = 0) {
  const [entry, setEntry] = useState<{ path: string | null; value: Resource<T> }>({ path, value: {} });
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);

  useEffect(() => {
    if (path === null) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      try {
        const data = await runtimeApi<T>(path!, undefined, controller.signal);
        if (!controller.signal.aborted) setEntry({ path, value: { data } });
      } catch (error) {
        if (!controller.signal.aborted) {
          setEntry(previous => ({ path, value: { data: previous.path === path ? previous.value.data : undefined, error: String(error) } }));
        }
      } finally {
        if (interval && !controller.signal.aborted) timer = setTimeout(read, interval);
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, interval, revision]);

  return { ...(entry.path === path ? entry.value : {}), refresh };
}

export type WorkbenchConfig = {
  workspace: string;
  provider: string | null;
  models: string[];
  conversation_model: string | null;
  development_model: string | null;
  development_shell: boolean;
  development_network: boolean;
};

export type TaskInput = {
  request_id: string;
  source: string;
  status: string;
  settled_sequence: number | null;
  result_excerpt: string | null;
  result_truncated: boolean;
  diagnostic: string | null;
  session_id: string | null;
  worktree: string | null;
};

export type Task = {
  task_id: string;
  prompt: string;
  source: string;
  mode: string;
  status: string;
  worktree: string | null;
  branch: string | null;
  session_id: string | null;
  conversation_key: string | null;
  attempts: number;
  sequence: number;
  inputs?: TaskInput[];
  result: string | null;
  diagnostic: string | null;
  metrics: Record<string, unknown>;
};

export type Message = {
  role: string;
  content?: string | null;
  tool_call_id?: string | null;
  tool_calls?: { id: string; name: string }[] | null;
};

export type ExecutionEvent = {
  sequence: number;
  kind: string;
  created_at?: string;
  data: Record<string, unknown>;
};
