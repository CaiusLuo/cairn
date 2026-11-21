import { RuntimeError } from "./runtime-api";

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export type KnowledgeDocument = {
  document_id: string;
  owner: string;
  kind: string;
  title: string;
  media_type: string;
  sha256: string;
  size_bytes: number;
  status: DocumentStatus;
  embedding_status: string;
  graph_status: string;
  error: string | null;
  attempts: number;
  created_at: string;
  updated_at: string;
  processed_at: string | null;
  metadata: Record<string, unknown>;
};

export type DocumentText = {
  document: KnowledgeDocument;
  content: string;
  offset: number;
  total_chars: number;
  next_offset: number | null;
};

export type DocumentChunk = {
  title: string;
  heading: string;
  content: string;
  offset: number;
};

function failureText(status: number, error?: string) {
  if (error === "document_source_not_found") return "来源已不可用。文档可能已删除或重新处理。";
  if (error === "document_not_found") return "文档不存在或已被删除。";
  if (error === "unsupported_document_type") return "暂不支持该文件类型，请上传 PDF、Office、HTML、Markdown 或纯文本。";
  if (error === "document_too_large") return "文件超过上传大小上限。";
  if (error === "invalid_document_upload") return "文件名无效或文件为空。";
  if (error === "invalid_knowledge_request") return "请求参数不符合要求。";
  if (error === "knowledge_store_unavailable") return "文档库暂时不可用，请稍后重试。";
  if (status === 500) return "Server 处理失败，请稍后重试或检查服务日志。";
  if (status === 503) return "Server 暂时不可用，请稍后重试或检查服务日志。";
  return error || `请求失败 (${status})`;
}

async function request<T>(path: string, init?: RequestInit, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/knowledge/documents${path}`, {
      signal,
      credentials: "same-origin",
      cache: "no-store",
      ...init,
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

export function listDocuments(limit: number, offset: number, signal?: AbortSignal) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return request<{ documents: KnowledgeDocument[] }>("?" + params, undefined, signal);
}

export function documentText(documentId: string, offset = 0, maxChars = 20_000, signal?: AbortSignal) {
  const params = new URLSearchParams({ offset: String(offset), max_chars: String(maxChars) });
  return request<DocumentText>(`/${encodeURIComponent(documentId)}/text?${params}`, undefined, signal);
}

export function documentChunk(documentId: string, chunkId: number, signal?: AbortSignal) {
  const params = new URLSearchParams({ chunk_id: String(chunkId) });
  return request<DocumentChunk>("/" + encodeURIComponent(documentId) + "/chunk?" + params, undefined, signal);
}

// The filename header is latin-1, so UTF-8 names travel percent-encoded.
export function uploadDocument(file: File) {
  return request<{ document: KnowledgeDocument; deduplicated: boolean }>("", {
    method: "POST",
    headers: {
      "content-type": file.type || "application/octet-stream",
      "x-cairn-filename": encodeURIComponent(file.name),
    },
    body: file,
  });
}

export function retryDocument(documentId: string) {
  return request<{ document: KnowledgeDocument }>(`/${encodeURIComponent(documentId)}/retry`, { method: "POST" });
}

export function deleteDocument(documentId: string) {
  return request<{ deleted: boolean }>(`/${encodeURIComponent(documentId)}`, { method: "DELETE" });
}
