export type SourceStatus = "ok" | "missing" | "unavailable";

export interface SessionSummary {
  session_id: string;
  short_id: string;
  state: "ready" | "executing" | "recovery_required";
  title: string | null;
  created_at_utc: string;
  updated_at_utc: string;
  head_seq: number;
  source: string | null;
  task_ids: string[];
  live: null;
  agent: null;
}

export interface SessionMessage {
  role: "user" | "assistant" | "tool";
  content: string;
}

export interface SessionDetail extends SessionSummary {
  messages: SessionMessage[];
  recovery_status: "clean" | "executing" | "effects_unknown" | "recovery_required";
}

export interface TaskTodo {
  todo_id: string;
  text: string;
  done: boolean;
}

export interface TaskItem {
  task_id: string;
  title: string;
  description: string;
  project: string;
  source: string;
  session_id: string | null;
  status: string;
  todos: TaskTodo[];
  progress: string[];
  issue_url: string | null;
  pr_url: string | null;
  created_at: string;
  updated_at: string;
  revision: number;
}

export interface Snapshot {
  version: number;
  sequence: number;
  generated_at_utc: string;
  read_only: true;
  source_status: { sessions: SourceStatus; tasks: SourceStatus };
  sessions: SessionSummary[];
  tasks: TaskItem[];
}

export async function fetchSnapshot(): Promise<Snapshot> {
  const response = await fetch("/api/snapshot", { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error(`snapshot ${response.status}`);
  return (await response.json()) as Snapshot;
}

export async function fetchSession(sessionId: string): Promise<SessionDetail> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    headers: { accept: "application/json" }
  });
  if (!response.ok) throw new Error(`session ${response.status}`);
  return (await response.json()) as SessionDetail;
}

export const sourceLabels: Record<string, string> = {
  local: "Local",
  cli: "CLI",
  wechat: "WeChat",
  schedule: "Schedule",
  github: "GitHub",
  web: "Web"
};

export const sourceClass: Record<string, string> = {
  local: "tag",
  cli: "tag tag--cli",
  wechat: "tag tag--wx",
  schedule: "tag tag--schedule",
  github: "tag tag--refactor",
  web: "tag tag--web"
};

export const stateLabels: Record<SessionSummary["state"], string> = {
  ready: "Ready",
  executing: "Executing",
  recovery_required: "Recovery required"
};

export function shortId(value: string, length = 8): string {
  return value.length > length ? value.slice(0, length) : value;
}

export function sourceLabel(source: string | null): string {
  return source ? sourceLabels[source] ?? source : "Unknown";
}

export function sourceClassName(source: string | null): string {
  return source ? sourceClass[source] ?? "tag" : "tag source-unknown";
}

export interface TaskUpdateResponse {
  task: TaskItem;
  sequence: number;
  generated_at_utc: string;
  execution_unchanged: true;
}

export class TaskUpdateError extends Error {
  readonly status: number;
  readonly code: string;
  readonly currentRevision: number | null;

  constructor(status: number, code: string, currentRevision: number | null = null) {
    super(code);
    this.name = "TaskUpdateError";
    this.status = status;
    this.code = code;
    this.currentRevision = currentRevision;
  }
}

export async function updateTaskStatus(
  taskId: string,
  status: string,
  expectedRevision: number
): Promise<TaskUpdateResponse> {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ status, expected_revision: expectedRevision })
  });
  const payload = (await response.json().catch(() => ({}))) as {
    error?: string;
    current_revision?: number;
  } & TaskUpdateResponse;
  if (!response.ok) {
    throw new TaskUpdateError(
      response.status,
      payload.error ?? `task_update_${response.status}`,
      typeof payload.current_revision === "number" ? payload.current_revision : null
    );
  }
  return payload;
}
