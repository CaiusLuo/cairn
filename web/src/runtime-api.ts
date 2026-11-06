export class RuntimeError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function runtimeApi(path: string, body?: unknown) {
  const response = await fetch(`/api/runtime/${path}`, { credentials: "same-origin", cache: "no-store", ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
  if (response.status === 401) { location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`); throw new RuntimeError(401, "请先登录"); }
  const value = await response.json().catch(() => ({}));
  if (response.status === 404 && /^(commands|management)/.test(path)) throw new RuntimeError(404, "当前 Server 仍在运行旧版本, 请重启 cairn 后重试");
  if (!response.ok) throw new RuntimeError(response.status, value.message || value.error || `请求失败 (${response.status})`);
  return value;
}
export function taskStatus(status: string) {
  return ({ queued: "排队中", running: "处理中", completed: "已完成", paused: "已暂停", cancelled: "已取消", failed: "失败", recovery_required: "需要核对后恢复" } as Record<string, string>)[status] ?? status;
}
