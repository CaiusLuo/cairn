import { RuntimeError } from "./runtime-api";

export type ModelKeySource = "host" | "dotenv" | "missing";

export type ModelPreset = {
  id: string;
  name: string;
  provider_name: string;
  base_url: string;
  model_ids: string[];
  note: string;
};

export type ModelGroup = {
  name: string;
  model_ids: string[];
  next_start: boolean;
  approved: boolean;
  active: boolean;
};

export type ModelProvider = {
  name: string;
  base_url: string;
  api_key_env: string;
  key_configured: boolean;
  key_source: ModelKeySource;
  next_start: boolean;
  approved: boolean;
  groups: ModelGroup[];
};

export type ModelChoice = {
  provider: string;
  group: string;
  model_id: string;
};

export type ModelDraft = {
  provider_name: string;
  base_url: string;
  api_key_env: string;
  group_name: string;
  model_ids: string[];
  key_configured: boolean;
  key_source: ModelKeySource;
};

export type ModelSettings = {
  workspace: string;
  config_path: string;
  approval_path: string | null;
  config_exists: boolean;
  layout: "env" | "legacy" | "catalog";
  providers: ModelProvider[];
  models: ModelChoice[];
  active_provider: string | null;
  active_group: string | null;
  active_model: string | null;
  running: {
    provider: string;
    group: string;
    model: string;
    base_url: string;
  } | null;
  offline: boolean;
  approval_valid: boolean;
  restart_required: boolean;
  restart_command: string;
  can_save: boolean;
  blocked_reason: string | null;
  draft: ModelDraft | null;
  presets: ModelPreset[];
};

export type SaveModelBody = {
  mode: "create" | "update";
  active_provider: string;
  provider_name: string;
  base_url: string;
  api_key_env: string | null;
  api_key: string | null;
  active_group: string;
  group_name: string;
  model_ids: string[];
};

export type SaveModelResult = {
  settings: ModelSettings;
  restart_required: boolean;
  restart_command: string;
  message: string;
};

export type SelectModelBody = {
  provider: string;
  group: string;
  model: string;
};

export type SelectModelResult = {
  settings: ModelSettings;
  message: string;
};

export type ProbeModelResult = {
  endpoint: string;
  models: string[];
};

export class ModelApiError extends RuntimeError {
  constructor(
    status: number,
    message: string,
    readonly code?: string,
  ) {
    super(status, message);
    this.name = "ModelApiError";
  }
}

const FAILURE_TEXT: Record<string, string> = {
  invalid_origin: "写操作必须来自同一 Web 会话，请刷新页面后重试。",
  invalid_model_settings:
    "模型配置不符合要求，请检查 Base URL、模型 ID、提供方名称和模型组名称。",
  model_config_invalid:
    "服务器上的 .cairn/models.toml 无法读取；请先在服务器上修复该文件。",
  model_settings_unavailable:
    "模型配置文件暂时不可写；请检查服务用户对工作区和 provider approval 的权限。",
  model_config_conflict:
    "配置文件在保存过程中被其他操作修改，请刷新后重新提交。",
  host_env_credential:
    "该 API Key 由服务环境提供，Web 不能覆盖；请更新服务的环境变量后重启。",
  credential_missing: "还没有可用的 API Key，请填写后再保存。",
  invalid_model_selection:
    "该模型不在当前已启用的提供方和模型组内；请先保存配置或刷新页面。",
  model_selection_unavailable: "当前没有运行中的提供方，无法切换模型。",
  model_settings_rollback_failed:
    "保存失败且回滚不完整；请立即在服务器上核对 models.toml、.env 和 provider approval。",
  probe_credential_rejected: "提供方拒绝了该 API Key，请检查 Key 或 endpoint。",
  probe_failed: "无法获取模型列表；可以直接手填模型 ID。",
  probe_timeout: "连接提供方超时；请检查 endpoint 或网络。",
  request_too_large: "请求内容过大。",
  invalid_json: "请求内容不是有效的 JSON。",
  unsupported_media_type: "请求内容类型不受支持。",
};

export function modelFailure(status: number, error?: string, message?: string) {
  if (message) return message;
  if (error && FAILURE_TEXT[error]) return FAILURE_TEXT[error];
  if (status === 500) return "Server 处理失败，请稍后重试或检查服务日志。";
  if (status === 503) return "Server 暂时不可用，请稍后重试或检查服务日志。";
  return error || `请求失败 (${status})`;
}

async function modelApi<T>(
  path: string,
  init: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/model${path}`, {
      method: init.method ?? (init.body === undefined ? "GET" : "PUT"),
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
    throw new ModelApiError(0, "无法连接 Server，请检查网络后重试。");
  }
  if (response.status === 401) {
    location.assign(
      `/login.html?next=${encodeURIComponent(location.pathname + location.search)}`,
    );
    throw new ModelApiError(401, "请先登录");
  }
  const value = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ModelApiError(
      response.status,
      modelFailure(response.status, value.error, value.message),
      value.error,
    );
  }
  return value as T;
}

export async function getModelSettings(
  signal?: AbortSignal,
): Promise<ModelSettings> {
  const value = await modelApi<{ settings: ModelSettings }>("", { signal });
  return value.settings;
}

export function saveModelSettings(body: SaveModelBody) {
  return modelApi<SaveModelResult>("", { method: "PUT", body });
}

export function selectModel(body: SelectModelBody) {
  return modelApi<SelectModelResult>("/select", { method: "POST", body });
}

export function probeModels(body: {
  base_url: string;
  api_key_env: string | null;
  api_key: string | null;
}) {
  return modelApi<ProbeModelResult>("/probe", { method: "POST", body });
}
