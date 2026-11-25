export type ParsedCurl = {
  endpoint: string | null;
  baseUrl: string | null;
  providerName: string | null;
  modelIds: string[];
  apiKey: string | null;
  warnings: string[];
  error: string | null;
};

const VALUE_FLAGS = new Set([
  "-H",
  "--header",
  "-d",
  "--data",
  "--data-raw",
  "--data-binary",
  "--data-ascii",
  "--data-urlencode",
  "--url",
  "-X",
  "--request",
  "-u",
  "--user",
  "-A",
  "--user-agent",
  "-b",
  "--cookie",
  "-e",
  "--referer",
  "-m",
  "--max-time",
  "--connect-timeout",
  "--retry",
  "--retry-delay",
  "--cacert",
  "--cert",
  "--key",
  "-o",
  "--output",
]);

const DATA_FLAGS = new Set([
  "-d",
  "--data",
  "--data-raw",
  "--data-binary",
  "--data-ascii",
  "--data-urlencode",
]);

const ENDPOINT_SUFFIX =
  /\/(chat\/completions|completions|responses|embeddings|messages|models)$/i;
const VERSIONED_PATH = /\/v\d+[a-z0-9/]*$/i;

function tokenize(command: string): string[] {
  const text = command.replace(/\\\r?\n/g, " ");
  const tokens: string[] = [];
  let current = "";
  let quote: "'" | '"' | null = null;
  let active = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quote === "'") {
      if (char === "'") quote = null;
      else current += char;
      continue;
    }
    if (quote === '"') {
      if (char === '"') quote = null;
      else if (char === "\\" && index + 1 < text.length) {
        index += 1;
        current += text[index];
      } else current += char;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      active = true;
      continue;
    }
    if (char === "\\" && index + 1 < text.length) {
      index += 1;
      current += text[index];
      active = true;
      continue;
    }
    if (/\s/.test(char)) {
      if (active) {
        tokens.push(current);
        current = "";
        active = false;
      }
      continue;
    }
    current += char;
    active = true;
  }
  if (active) tokens.push(current);
  return tokens;
}

function headerValue(headers: string[], name: string): string | null {
  const wanted = name.toLowerCase();
  for (let index = headers.length - 1; index >= 0; index -= 1) {
    const raw = headers[index];
    const separator = raw.indexOf(":");
    if (separator === -1) continue;
    if (raw.slice(0, separator).trim().toLowerCase() !== wanted) continue;
    return raw.slice(separator + 1).trim() || null;
  }
  return null;
}

function isPlaceholder(value: string): boolean {
  return (
    /^(<|\{\{|\$|%)/.test(value) ||
    /your[-_ ]?api[-_ ]?key|api[-_ ]?key[-_ ]?here/i.test(value) ||
    value.includes("...")
  );
}

function credential(headers: string[], warnings: string[]): string | null {
  const authorization = headerValue(headers, "authorization");
  if (authorization) {
    if (/^bearer\s+/i.test(authorization)) {
      return authorization.replace(/^bearer\s+/i, "").trim() || null;
    }
    if (/^(basic|digest|negotiate)\s/i.test(authorization)) {
      warnings.push("不支持的 Authorization 类型，请手动填写 API Key。");
      return null;
    }
    return authorization;
  }
  return (
    headerValue(headers, "api-key") ??
    headerValue(headers, "x-api-key") ??
    headerValue(headers, "x-goog-api-key")
  );
}

function bodyModelIds(body: string, endpoint: string): string[] {
  const modelIds: string[] = [];
  const trimmed = body.trim();
  if (trimmed) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (
        parsed &&
        typeof parsed === "object" &&
        typeof (parsed as { model?: unknown }).model === "string"
      ) {
        modelIds.push((parsed as { model: string }).model.trim());
      }
    } catch {
      // Not strict JSON; the regex below still covers copied documentation.
    }
    const match = trimmed.match(/["']model["']\s*:\s*["']([^"']+)["']/);
    if (match) modelIds.push(match[1].trim());
    if (!modelIds.length && /(^|&)model=/.test(trimmed)) {
      const model = new URLSearchParams(trimmed).get("model");
      if (model) modelIds.push(model.trim());
    }
  }
  try {
    const url = new URL(endpoint);
    for (const key of ["model", "model_name"]) {
      const value = url.searchParams.get(key);
      if (value) modelIds.push(value.trim());
    }
    const deployment = url.pathname.match(/\/deployments\/([^/]+)\//);
    if (deployment) modelIds.push(decodeURIComponent(deployment[1]));
    const pathModel = url.pathname.match(/\/models\/([^/:?#]+)(?::[^/?#]+)?$/);
    if (pathModel) modelIds.push(decodeURIComponent(pathModel[1]));
  } catch {
    // The caller reports an invalid endpoint separately.
  }
  return [...new Set(modelIds.filter(Boolean))];
}

function deriveBase(endpoint: string): { base: string | null; note: string | null } {
  let url: URL;
  try {
    url = new URL(endpoint);
  } catch {
    return { base: null, note: "请求 URL 不是有效的 http(s) 地址。" };
  }
  const host = url.hostname.toLowerCase();
  const path = url.pathname;
  if (host.endsWith(".openai.azure.com") || path.includes("/openai/deployments/")) {
    return {
      base: null,
      note: "检测到 Azure OpenAI 专用地址；当前表单仅支持 OpenAI 兼容 Base URL。",
    };
  }
  if (host === "generativelanguage.googleapis.com" || /:generateContent$/i.test(path)) {
    return {
      base: null,
      note: "检测到 Gemini 原生地址；请改填其 OpenAI 兼容 Base URL。",
    };
  }
  if (
    /^dashscope(-intl)?\.aliyuncs\.com$/i.test(host) &&
    path.includes("/api/v1/services/")
  ) {
    return {
      base: `${url.protocol}//${url.host}/compatible-mode/v1`,
      note: "已将百炼原生接口转换为 OpenAI 兼容 Base URL。",
    };
  }
  if (
    host === "api.minimax.chat" &&
    /\/v1\/text\/chatcompletion_v2$/i.test(path)
  ) {
    return {
      base: `${url.protocol}//${url.host}/v1`,
      note: "已将 MiniMax 原生接口转换为 OpenAI 兼容 Base URL。",
    };
  }
  let normalized = path.replace(/\/+$/, "");
  const known = ENDPOINT_SUFFIX.test(normalized);
  if (known) normalized = normalized.replace(ENDPOINT_SUFFIX, "");
  normalized = normalized.replace(/\/+$/, "");
  url.pathname = normalized || "/";
  url.search = "";
  url.hash = "";
  const base = url.toString().replace(/\/$/, "");
  return {
    base,
    note:
      known || !normalized || VERSIONED_PATH.test(normalized)
        ? null
        : "未识别 chat/completions 后缀，已按请求地址推断 Base URL，请核对。",
  };
}

function providerNameFrom(baseUrl: string | null): string | null {
  if (!baseUrl) return null;
  try {
    const host = new URL(baseUrl).hostname.toLowerCase();
    if (
      host === "localhost" ||
      host.includes(":") ||
      /^\d+(?:\.\d+){3}$/.test(host)
    ) {
      return null;
    }
    const labels = host.split(".");
    let candidate = labels[0];
    if (["api", "open", "www"].includes(candidate) && labels[1]) {
      candidate = labels[1];
    }
    candidate = candidate
      .replace(/[^a-z0-9-]+/g, "-")
      .replace(/^-+|-+$/g, "");
    return candidate ? candidate.slice(0, 48) : null;
  } catch {
    return null;
  }
}

export function parseCurl(command: string): ParsedCurl {
  const tokens = tokenize(command);
  if (!tokens.length) {
    return {
      endpoint: null,
      baseUrl: null,
      providerName: null,
      modelIds: [],
      apiKey: null,
      warnings: [],
      error: "请粘贴 curl 命令。",
    };
  }
  const warnings: string[] = [];
  const headers: string[] = [];
  const data: string[] = [];
  let endpoint: string | null = null;

  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (!token || token.toLowerCase() === "curl") continue;
    let flag: string | null = null;
    let value: string | null = null;
    if (token.startsWith("--")) {
      const separator = token.indexOf("=");
      if (separator !== -1) {
        flag = token.slice(0, separator);
        value = token.slice(separator + 1);
      } else {
        flag = token;
        if (VALUE_FLAGS.has(flag) && index + 1 < tokens.length) {
          index += 1;
          value = tokens[index];
        }
      }
    } else if (/^-[A-Za-z]/.test(token)) {
      flag = token.slice(0, 2);
      const rest = token.slice(2);
      if (VALUE_FLAGS.has(flag)) {
        if (rest) value = rest;
        else if (index + 1 < tokens.length) {
          index += 1;
          value = tokens[index];
        }
      }
    }
    if (flag === "--url" && value) endpoint = value;
    else if ((flag === "-H" || flag === "--header") && value) headers.push(value);
    else if (flag && DATA_FLAGS.has(flag) && value) data.push(value);
    else if (!flag && !endpoint && /^https?:\/\//i.test(token)) endpoint = token;
  }

  if (!endpoint) {
    return {
      endpoint: null,
      baseUrl: null,
      providerName: null,
      modelIds: [],
      apiKey: null,
      warnings,
      error: "没有在 curl 中找到请求 URL（http:// 或 https://）。",
    };
  }
  if (!/^https?:\/\//i.test(endpoint)) {
    return {
      endpoint,
      baseUrl: null,
      providerName: null,
      modelIds: [],
      apiKey: null,
      warnings,
      error: "请求 URL 必须使用 http:// 或 https://。",
    };
  }
  try {
    new URL(endpoint);
  } catch {
    return {
      endpoint,
      baseUrl: null,
      providerName: null,
      modelIds: [],
      apiKey: null,
      warnings,
      error: "请求 URL 不是有效的 http(s) 地址。",
    };
  }

  const derived = deriveBase(endpoint);
  if (derived.note) warnings.push(derived.note);
  if (derived.base?.startsWith("http://") && !/^http:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/i.test(derived.base)) {
    warnings.push("远程 Base URL 需要使用 HTTPS，保存时服务器会校验。");
  }
  let apiKey = credential(headers, warnings);
  if (apiKey && isPlaceholder(apiKey)) {
    warnings.push("curl 中的 API Key 是占位符，请手动填写。");
    apiKey = null;
  }
  if (!apiKey) warnings.push("未找到可用的 API Key，请手动填写。");
  const modelIds = bodyModelIds(data.join(""), endpoint);
  if (!modelIds.length) {
    warnings.push("未找到模型 ID，请手动填写或保存后探测模型。");
  }
  return {
    endpoint,
    baseUrl: derived.base,
    providerName: providerNameFrom(derived.base),
    modelIds,
    apiKey,
    warnings,
    error: null,
  };
}
