import { RuntimeError } from "./runtime-api";

export type Clip = {
  clip_id: string;
  content: string;
  source: string;
  tags: string[];
  content_hash: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
};

function failureText(status: number, error?: string) {
  if (error === "clip_not_found") return "知识片段不存在或已被删除。";
  if (error === "request_id_conflict") return "该请求此前保存了不同内容，请重新提交。";
  if (error === "invalid_knowledge_request") return "内容或标签不符合要求，请检查后重试。";
  if (error === "knowledge_store_unavailable") return "知识存储暂时不可用，请稍后重试。";
  if (status === 500) return "Server 处理失败，请稍后重试或检查服务日志。";
  if (status === 503) return "Server 暂时不可用，请稍后重试或检查服务日志。";
  return error || `请求失败 (${status})`;
}

export async function knowledgeApi<T = unknown>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/knowledge/${path}`, {
      signal,
      credentials: "same-origin",
      cache: "no-store",
      ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
  } catch (reason) {
    if (signal?.aborted) throw reason;
    throw new RuntimeError(0, "无法连接 Server，请检查网络后重试。");
  }
  if (response.status === 401) {
    location.assign(`/login.html?next=${encodeURIComponent(location.pathname + location.search)}`);
    throw new RuntimeError(401, "请先登录");
  }
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new RuntimeError(response.status, value.message || failureText(response.status, value.error));
  return value as T;
}
