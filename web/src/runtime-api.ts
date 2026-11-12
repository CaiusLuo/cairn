export class RuntimeError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
function failureText(path: string, status: number, error?: string) {
  if (status === 404 && /^conversations\/[^/]+$/.test(path)) return "对话不存在或已被删除。";
  if (status === 500) return "Server 处理失败，请稍后重试或检查服务日志。";
  if (status === 503) return "Server 暂时不可用，请稍后重试或检查服务日志。";
  return error || `请求失败 (${status})`;
}
export async function runtimeApi<T = any>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/runtime/${path}`, { signal, credentials: "same-origin", cache: "no-store", ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
  } catch (reason) {
    if (signal?.aborted) throw reason;
    throw new RuntimeError(0, "无法连接 Server，请检查网络后重试。");
  }
  if (response.status === 401) { location.assign(`/login.html?next=${encodeURIComponent(location.pathname + location.search)}`); throw new RuntimeError(401, "请先登录"); }
  const value = await response.json().catch(() => ({}));
  if (response.status === 404 && /^(commands|management)/.test(path)) throw new RuntimeError(404, "当前 Server 仍在运行旧版本, 请重启 cairn 后重试");
  if (!response.ok) throw new RuntimeError(response.status, value.message || failureText(path, response.status, value.error));
  return value;
}
export function taskStatus(status: string) {
  return ({ queued: "排队中", running: "处理中", completed: "已完成", paused: "已暂停", cancelled: "已取消", failed: "失败", recovery_required: "需要核对后恢复" } as Record<string, string>)[status] ?? status;
}
