import { RuntimeError } from "./runtime-api";

export type ChannelPlatform = "wechat" | "qq" | "telegram" | "discord";
export type ChannelDisplayState = "unconfigured" | "running" | "silent";
export type ChannelActualState = "running" | "stopped" | "connecting" | "error";
export type ChannelDesiredState = "running" | "stopped";

export type ChannelConnection = {
  connection_id: string;
  platform: ChannelPlatform;
  display_name: string;
  enabled: boolean;
  desired_state: ChannelDesiredState;
  credential_ref: string | null;
  owner_identity: string | null;
  allowed_senders: string[];
  metadata: Record<string, unknown>;
  revision: number;
  created_at: string;
  updated_at: string;
  legacy: boolean;
};

export type ChannelRuntime = {
  connection_id: string;
  actual_state: ChannelActualState;
  last_connected_at: string | null;
  last_heartbeat_at: string | null;
  last_error: string | null;
  next_retry_at: string | null;
};

export type ChannelView = {
  connection: ChannelConnection;
  runtime: ChannelRuntime;
};

export type ChannelPlatformSummary = {
  platform: ChannelPlatform;
  display_state: ChannelDisplayState;
  connections: number;
  running: number;
};

export type ChannelCredentialField = {
  key: string;
  label: string;
  required: boolean;
  secret: boolean;
  placeholder: string | null;
};

export type ChannelCapabilities = {
  platform: ChannelPlatform;
  configured: boolean;
  adapter_supported: boolean;
  legacy_managed: boolean;
  credential_schema: ChannelCredentialField[];
  supported_actions: string[];
};

export type ChannelOverview = {
  platforms: ChannelPlatformSummary[];
  connections: ChannelView[];
};

export class ChannelApiError extends RuntimeError {
  constructor(
    status: number,
    message: string,
    readonly code?: string,
  ) {
    super(status, message);
    this.name = "ChannelApiError";
  }
}

const FAILURE_TEXT: Record<string, string> = {  invalid_channel_request: "连接参数不符合要求，请检查名称、凭据或发送者列表。",
  invalid_json: "请求内容不是有效的 JSON。",
  unsupported_media_type: "请求内容类型不受支持。",
  channel_request_too_large: "请求内容过大。",
  channel_not_found: "连接不存在或已被删除。",
  legacy_channel_managed: "微信连接由宿主登录状态管理，不能通过普通连接操作修改。",
  revision_conflict: "连接已被其他操作更新，请刷新后重试。",
  request_id_conflict: "该请求编号此前对应了不同操作，请重新发起。",
  channel_operation_uncertain: "操作结果未知，Server 可能已经受理。请先刷新核对，重试会沿用同一请求编号。",
  channel_delivery_uncertain: "连接正在发送消息，结果未知，已停止自动重试。",
  channel_disabled: "该连接已停用，请先启用。",
  confirmation_required: "该操作需要明确确认。",
  channel_credentials_invalid: "凭据无效或缺失，请重新填写。",
  adapter_not_implemented: "Server 尚未实现该平台的 Adapter；配置可以保存，但暂时无法启动。",
  channel_adapter_error: "平台 Adapter 操作失败，请检查最近错误。",
  channel_store_unavailable: "连接存储暂时不可用，请稍后重试。",
  invalid_origin: "写操作必须来自同一 Web 会话。",
  explicit_credential_replacement_required: "替换凭据需要明确确认。",
  credentials_required: "请填写要替换的凭据。",
};

export function channelFailure(status: number, error?: string) {
  if (error && FAILURE_TEXT[error]) return FAILURE_TEXT[error];
  if (status === 500) return "Server 处理失败，请稍后重试或检查服务日志。";
  if (status === 503) return "Server 暂时不可用，请稍后重试或检查服务日志。";
  return error || `请求失败 (${status})`;
}

export async function channelApi<T = unknown>(
  path: string,
  init: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/channels${path}`, {
      method: init.method ?? (init.body === undefined ? "GET" : "POST"),
      signal: init.signal,
      credentials: "same-origin",
      cache: "no-store",
      ...(init.body === undefined
        ? {}
        : {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(init.body),
          }),
    });
  } catch (reason) {
    if (init.signal?.aborted) throw reason;
    throw new RuntimeError(0, "无法连接 Server，请检查网络后重试。");
  }
  if (response.status === 401) {
    location.assign(
      `/login.html?next=${encodeURIComponent(location.pathname + location.search)}`,
    );
    throw new RuntimeError(401, "请先登录");
  }
  const value = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ChannelApiError(
      response.status,
      channelFailure(response.status, value.error),
      value.error,
    );
  }
  return value as T;
}

export function getChannelOverview(signal?: AbortSignal) {
  return channelApi<ChannelOverview>("", { signal });
}

export function getChannelCapabilities(signal?: AbortSignal) {
  return channelApi<{ platforms: ChannelCapabilities[] }>("/capabilities", {
    signal,
  });
}

export function createChannelConnection(
  platform: ChannelPlatform,
  body: {
    request_id: string;
    display_name: string;
    enabled: boolean;
    owner_identity: string | null;
    allowed_senders: string[];
    credentials?: Record<string, string>;
  },
) {
  return channelApi<ChannelView & { request_id: string }>(
    `/${platform}/connections`,
    { body },
  );
}

export function updateChannelConnection(
  connectionId: string,
  body: {
    expected_revision: number;
    request_id: string;
    display_name?: string;
    enabled?: boolean;
    owner_identity?: string | null;
    allowed_senders?: string[];
    credentials?: Record<string, string>;
    replace_credentials?: boolean;
  },
) {
  return channelApi<ChannelView & { request_id: string }>(
    `/connections/${encodeURIComponent(connectionId)}`,
    { method: "PATCH", body },
  );
}

export function deleteChannelConnection(
  connectionId: string,
  body: { expected_revision: number; request_id: string; confirm: true },
) {
  return channelApi<{
    request_id: string;
    deleted: true;
    connection_id: string;
    audit_retained: true;
    replayed: boolean;
  }>(`/connections/${encodeURIComponent(connectionId)}`, {
    method: "DELETE",
    body,
  });
}

export function channelAction(
  connectionId: string,
  action: "start" | "stop" | "reconnect",
  requestId: string,
) {
  return channelApi<ChannelView & { request_id: string }>(
    `/connections/${encodeURIComponent(connectionId)}/${action}`,
    { body: { request_id: requestId } },
  );
}
