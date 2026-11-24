import { useCallback, useEffect, useState, type FormEvent } from "react";
import { parseCurl } from "../curl-import";
import {
  getModelSettings,
  probeModels,
  saveModelSettings,
  type ModelKeySource,
  type ModelPreset,
  type ModelProvider,
  type ModelSettings,
} from "../model-api";
import { PageHeader } from "./execution";

type Draft = {
  mode: "create" | "update";
  originalProvider: string;
  originalGroup: string;
  providerName: string;
  baseUrl: string;
  apiKeyEnv: string;
  apiKey: string;
  groupName: string;
  modelIdsText: string;
  keyConfigured: boolean;
  keySource: ModelKeySource;
};

const KEY_ENV_DEFAULT = "CAIRN_LLM_API_KEY";

const BLOCKED_TEXT: Record<string, string> = {
  approval_path_missing:
    "Server 没有配置 provider_approval（通常是离线验收模式），Web 不能保存模型配置。",
  config_not_writable:
    "工作区的 .cairn 目录不可写；请检查服务用户权限。",
  env_not_writable: "工作区 .env 不可写；请检查服务用户权限。",
  approval_not_writable:
    "provider approval 文件不可写；请让服务用户拥有该文件。",
  state_not_writable: "运行时状态目录不可写；请检查 data_root 权限。",
};

function parseIds(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map(item => item.trim())
    .filter(Boolean);
}

function providerFor(
  settings: ModelSettings,
  name: string,
): ModelProvider | undefined {
  return settings.providers.find(provider => provider.name === name);
}

function groupFor(provider: ModelProvider, name?: string | null) {
  return (
    provider.groups.find(group => group.name === name) ??
    provider.groups.find(group => group.next_start) ??
    provider.groups[0]
  );
}

function uniqueProviderName(settings: ModelSettings, base: string): string {
  const names = new Set(settings.providers.map(provider => provider.name));
  if (!names.has(base)) return base;
  let suffix = 2;
  while (names.has(`${base}-${suffix}`)) suffix += 1;
  return `${base}-${suffix}`;
}

function presetFor(
  presets: ModelPreset[],
  baseUrl: string | null,
): ModelPreset | undefined {
  if (!baseUrl) return undefined;
  const normalized = baseUrl.replace(/\/+$/, "").toLowerCase();
  const exact = presets.find(
    preset =>
      preset.base_url.replace(/\/+$/, "").toLowerCase() === normalized,
  );
  if (exact) return exact;
  try {
    const url = new URL(baseUrl);
    const path = url.pathname.replace(/\/+$/, "");
    if (path && path !== "/") return undefined;
    return presets.find(preset => {
      if (!preset.base_url) return false;
      try {
        return new URL(preset.base_url).host.toLowerCase() === url.host.toLowerCase();
      } catch {
        return false;
      }
    });
  } catch {
    return undefined;
  }
}

function Help({ text }: { text: string }) {
  return (
    <span
      className="model-help"
      role="img"
      aria-label={text}
      data-tip={text}
      tabIndex={0}
    >
      ?
    </span>
  );
}

function draftFromProvider(
  provider: ModelProvider,
  groupName?: string | null,
): Draft {
  const group = groupFor(provider, groupName);
  return {
    mode: "update",
    originalProvider: provider.name,
    originalGroup: group?.name ?? "",
    providerName: provider.name,
    baseUrl: provider.base_url,
    apiKeyEnv: provider.api_key_env,
    apiKey: "",
    groupName: group?.name ?? "",
    modelIdsText: group?.model_ids.join("\n") ?? "",
    keyConfigured: provider.key_configured,
    keySource: provider.key_source,
  };
}

function newDraft(settings: ModelSettings, preset?: ModelPreset): Draft {
  const base = settings.draft;
  const providerName = uniqueProviderName(
    settings,
    preset?.provider_name ?? base?.provider_name ?? "custom",
  );
  const suggested = preset?.model_ids ?? base?.model_ids ?? [];
  return {
    mode: "create",
    originalProvider: "",
    originalGroup: "",
    providerName,
    baseUrl: preset?.base_url ?? base?.base_url ?? "",
    apiKeyEnv:
      settings.providers.length === 0 ? (base?.api_key_env ?? "") : "",
    apiKey: "",
    groupName: preset ? `${preset.provider_name}-default` : "default",
    modelIdsText: suggested.join("\n"),
    keyConfigured:
      settings.providers.length === 0 ? (base?.key_configured ?? false) : false,
    keySource:
      settings.providers.length === 0 ? (base?.key_source ?? "missing") : "missing",
  };
}

function initialDraft(settings: ModelSettings): Draft {
  if (settings.providers.length === 0) return newDraft(settings);
  const provider =
    providerFor(settings, settings.active_provider ?? "") ??
    settings.providers[0];
  return draftFromProvider(provider, settings.active_group);
}

function keyHint(draft: Draft): string {
  if (draft.apiKey) {
    return "保存时会写入该 API Key；输入框内容不会回显。";
  }
  if (draft.keySource === "host") {
    return `当前密钥由服务环境变量 ${draft.apiKeyEnv} 提供；Web 不能覆盖它。`;
  }
  if (draft.keyConfigured) {
    return `已配置 ${draft.apiKeyEnv || "凭据"}；留空表示保持不变。`;
  }
  if (draft.mode === "create") {
    return "新提供方通常需要填写 API Key。留空时服务器会尝试自动选择凭据变量。";
  }
  return "尚未配置 API Key；保存前需要填写。";
}

function blockedText(reason: string | null): string {
  if (!reason) return "";
  return BLOCKED_TEXT[reason] ?? "当前环境不允许保存模型配置。";
}

export function ModelPage() {
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [presetId, setPresetId] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [savedRestart, setSavedRestart] = useState(false);
  const [probeMessage, setProbeMessage] = useState("");
  const [probed, setProbed] = useState<string[]>([]);
  const [curlText, setCurlText] = useState("");
  const [curlFeedback, setCurlFeedback] = useState("");
  const [curlFailed, setCurlFailed] = useState(false);

  const load = useCallback(async (resetDraft: boolean) => {
    setError("");
    try {
      const value = await getModelSettings();
      setSettings(value);
      if (resetDraft) {
        setDraft(initialDraft(value));
        setDirty(false);
        setPresetId("");
        setProbed([]);
        setProbeMessage("");
        setCurlText("");
        setCurlFeedback("");
        setCurlFailed(false);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  useEffect(() => {
    void load(true);
  }, [load]);

  function replaceDraft(next: Draft) {
    setDraft(next);
    setDirty(false);
    setPresetId("");
    setProbed([]);
    setProbeMessage("");
    setCurlText("");
    setCurlFeedback("");
    setCurlFailed(false);
  }

  function chooseProvider(provider: ModelProvider) {
    if (!settings) return;
    if (dirty && !window.confirm("当前修改尚未保存，仍要切换提供方吗？")) return;
    replaceDraft(draftFromProvider(provider));
  }

  function startNewProvider(preset?: ModelPreset) {
    if (!settings) return;
    if (dirty && !window.confirm("当前修改尚未保存，仍要新增提供方吗？")) return;
    replaceDraft(newDraft(settings, preset));
    setPresetId(preset?.id ?? "");
  }

  function chooseGroup(name: string) {
    if (!settings || !draft || draft.mode !== "update") return;
    if (dirty && !window.confirm("当前修改尚未保存，仍要切换模型组吗？")) return;
    const provider = providerFor(settings, draft.originalProvider);
    if (!provider) return;
    replaceDraft(draftFromProvider(provider, name));
  }

  function update(changes: Partial<Draft>) {
    setDraft(current => (current ? { ...current, ...changes } : current));
    setDirty(true);
    setSavedRestart(false);
  }

  function importCurl() {
    if (!draft || !settings) return;
    const parsed = parseCurl(curlText);
    setCurlFailed(false);
    if (parsed.error) {
      setCurlFailed(true);
      setCurlFeedback(parsed.error);
      return;
    }
    const preset = presetFor(settings.presets, parsed.baseUrl);
    const baseUrl = preset?.base_url || parsed.baseUrl;
    const suggested =
      preset?.provider_name ??
      parsed.providerName ??
      draft.providerName ??
      "custom";
    const providerName =
      draft.mode === "create"
        ? uniqueProviderName(settings, suggested.trim() || "custom")
        : draft.providerName;
    const modelIds = parsed.modelIds.length
      ? parsed.modelIds
      : preset?.model_ids ?? [];
    const changes: Partial<Draft> = {};
    if (baseUrl) changes.baseUrl = baseUrl;
    if (parsed.apiKey) changes.apiKey = parsed.apiKey;
    if (modelIds.length) changes.modelIdsText = modelIds.join("\n");
    if (draft.mode === "create") {
      changes.providerName = providerName;
      changes.groupName = `${providerName}-default`;
    }
    if (Object.keys(changes).length) update(changes);
    setCurlText("");
    const filled = [
      baseUrl ? "Base URL" : "",
      parsed.apiKey ? "API Key" : "",
      modelIds.length ? `${modelIds.length} 个模型 ID` : "",
    ].filter(Boolean);
    setCurlFeedback(
      [
        filled.length ? `已填入 ${filled.join("、")}` : "没有可填入的字段",
        ...parsed.warnings,
      ].join("；"),
    );
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!draft || busy || !settings?.can_save) return;
    const modelIds = parseIds(draft.modelIdsText);
    if (!draft.providerName.trim() || !draft.baseUrl.trim()) {
      setError("请填写提供方名称和 Base URL。");
      return;
    }
    if (!draft.groupName.trim()) {
      setError("请填写模型组名称。");
      return;
    }
    if (!modelIds.length) {
      setError("至少需要一个模型 ID。");
      return;
    }
    setBusy(true);
    setError("");
    setFeedback("");
    try {
      const result = await saveModelSettings({
        mode: draft.mode,
        active_provider: draft.originalProvider,
        provider_name: draft.providerName.trim(),
        base_url: draft.baseUrl.trim(),
        api_key_env: draft.apiKeyEnv.trim() || null,
        api_key: draft.apiKey || null,
        active_group: draft.originalGroup,
        group_name: draft.groupName.trim(),
        model_ids: modelIds,
      });
      setSettings(result.settings);
      setDraft(initialDraft(result.settings));
      setDirty(false);
      setPresetId("");
      setProbed([]);
      setProbeMessage("");
      setFeedback(result.message);
      setSavedRestart(result.restart_required);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function probe() {
    if (!draft || busy) return;
    setBusy(true);
    setError("");
    setProbeMessage("");
    setProbed([]);
    try {
      const result = await probeModels({
        base_url: draft.baseUrl.trim(),
        api_key_env: draft.apiKeyEnv.trim() || null,
        api_key: draft.apiKey || null,
      });
      setProbed(result.models);
      setProbeMessage(
        result.models.length
          ? `从 ${result.endpoint} 获取到 ${result.models.length} 个模型。`
          : `已连接 ${result.endpoint}，但没有返回模型 ID。`,
      );
      if (!parseIds(draft.modelIdsText).length && result.models.length) {
        update({ modelIdsText: result.models[0] });
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  function addProbed(modelId: string) {
    if (!draft || !modelId) return;
    const ids = parseIds(draft.modelIdsText);
    if (ids.includes(modelId)) return;
    update({ modelIdsText: [...ids, modelId].join("\n") });
  }

  const provider =
    settings && draft?.mode === "update"
      ? providerFor(settings, draft.originalProvider)
      : undefined;
  const running = settings?.running;
  const selectedPreset = settings?.presets.find(
    item => item.id === presetId,
  );
  const canSave = Boolean(settings?.can_save) && !busy;

  return (
    <div className="wb-page model-page">
      <PageHeader
        icon="sparkles"
        title="模型"
        description="配置 OpenAI 兼容的提供方和模型。优先提供国内模型预设；保存后立即用于新的对话回合。"
        actions={
          <button
            disabled={busy}
            onClick={() => {
              if (dirty && !window.confirm("当前修改尚未保存，仍要刷新吗？")) return;
              void load(true);
            }}
          >
            刷新
          </button>
        }
      />

      {error && (
        <p role="alert" className="control-error">
          {error}
        </p>
      )}
      {feedback && (
        <div className="control-feedback model-feedback" role="status">
          <span>{feedback}</span>
          {savedRestart && settings && (
            <code>{settings.restart_command}</code>
          )}
        </div>
      )}
      {!settings && !error && <p>正在读取模型配置…</p>}

      {settings && (
        <>
          {!settings.can_save && (
            <p className="model-warning" role="status">
              {blockedText(settings.blocked_reason)}
            </p>
          )}
          {settings.restart_required && !savedRestart && (
            <p className="model-warning" role="status">
              有已保存但尚未生效的模型修改。执行{" "}
              <code>{settings.restart_command}</code> 后会使用新的提供方和模型。
            </p>
          )}
          {!settings.approval_valid && settings.approval_path && (
            <p className="model-warning" role="status">
              当前 provider approval 无效或缺失；保存模型配置会重新生成它。
            </p>
          )}
          {settings.offline && (
            <p className="model-note">
              当前是离线验收模式；保存的模型配置只会在恢复在线配置并重启后使用。
            </p>
          )}

          <section className="model-status-grid" aria-label="模型运行状态">
            <article className="wb-card">
              <h2>当前运行</h2>
              {running ? (
                <dl>
                  <div>
                    <dt>提供方</dt>
                    <dd>{running.provider}</dd>
                  </div>
                  <div>
                    <dt>模型组</dt>
                    <dd>{running.group}</dd>
                  </div>
                  <div>
                    <dt>模型</dt>
                    <dd>
                      <code>{running.model}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Endpoint</dt>
                    <dd>
                      <code>{running.base_url}</code>
                    </dd>
                  </div>
                </dl>
              ) : (
                <p className="wb-muted">离线模式，当前没有加载提供方。</p>
              )}
            </article>
            <article className="wb-card">
              <h2>默认配置</h2>
              <dl>
                <div>
                  <dt>提供方</dt>
                  <dd>{settings.active_provider ?? "尚未配置"}</dd>
                </div>
                <div>
                  <dt>模型组</dt>
                  <dd>{settings.active_group ?? "尚未配置"}</dd>
                </div>
                <div>
                  <dt>模型</dt>
                  <dd>
                    {settings.active_model ? (
                      <code>{settings.active_model}</code>
                    ) : (
                      "尚未配置"
                    )}
                  </dd>
                </div>
              </dl>
            </article>
            <article className="wb-card">
              <h2>配置文件</h2>
              <dl>
                <div>
                  <dt>格式</dt>
                  <dd>{settings.layout === "catalog" ? "多提供方目录" : settings.layout === "legacy" ? "旧版单提供方" : ".env"}</dd>
                </div>
                <div>
                  <dt>models.toml</dt>
                  <dd>
                    <code>{settings.config_path}</code>
                  </dd>
                </div>
                <div>
                  <dt>approval</dt>
                  <dd>
                    <code>{settings.approval_path ?? "未配置"}</code>
                  </dd>
                </div>
              </dl>
            </article>
          </section>

          <section className="settings-section">
            <div className="model-section-head">
              <h2>提供方</h2>
              <Help text="新的对话回合使用列表中的第一个提供方及其第一个模型组。保存另一个提供方或模型组会把它移到第一位并立即生效，同时批准该提供方的全部模型组；对话框的模型菜单随后可按组切换。" />
            </div>
            <div className="model-provider-list">
              {settings.providers.map(item => (
                <button
                  type="button"
                  key={item.name}
                  className="model-provider"
                  aria-pressed={
                    draft?.mode === "update" &&
                    draft.originalProvider === item.name
                  }
                  onClick={() => chooseProvider(item)}
                >
                  <span className="model-provider-head">
                    <strong>{item.name}</strong>
                    {item.next_start && <em>默认</em>}
                  </span>
                  <code>{item.base_url}</code>
                  <span className="wb-muted">
                    {item.key_configured ? "密钥已配置" : "密钥未配置"} ·{" "}
                    {item.groups.length} 个模型组
                    {item.groups.some(group => group.approved) &&
                      ` · ${item.groups.filter(group => group.approved).length} 个已批准`}
                  </span>
                </button>
              ))}
              <button
                type="button"
                className="model-provider model-provider-new"
                aria-pressed={draft?.mode === "create"}
                onClick={() => startNewProvider()}
              >
                <span className="model-provider-head">
                  <strong>+ 新增提供方</strong>
                </span>
                <span className="wb-muted">
                  从国内模型预设开始，或填写自定义 endpoint。
                </span>
              </button>
            </div>
            {settings.providers.length === 0 && (
              <p className="model-note">
                当前由工作区 .env 提供模型设置。保存一个提供方后，会写入 .cairn/models.toml。
              </p>
            )}
          </section>

          {draft && (
            <section className="settings-section model-editor">
              <div className="model-section-head">
                <h2>
                  {draft.mode === "create"
                    ? "新增提供方"
                    : `编辑 ${draft.originalProvider}`}
                </h2>
                <Help text="选择预设或从 curl 导入会新建一个提供方，不会覆盖现有配置；保存后立即成为新回合使用的提供方。" />
              </div>
              <div className="model-curl">
                <div className="model-curl-head">
                  <strong>从 curl 自动填充</strong>
                  <span className="model-note">
                    只在本浏览器解析；成功后原始 curl 会被清除
                  </span>
                </div>
                <textarea
                  aria-label="curl 命令"
                  rows={4}
                  spellCheck={false}
                  placeholder={`curl https://api.example.com/v1/chat/completions \\\n  -H "Authorization: Bearer sk-..." \\\n  -H "Content-Type: application/json" \\\n  -d '{"model": "my-model", "messages": []}'`}
                  value={curlText}
                  onChange={event => {
                    setCurlText(event.target.value);
                    setCurlFeedback("");
                    setCurlFailed(false);
                  }}
                />
                <div className="model-curl-actions">
                  <button
                    type="button"
                    disabled={!curlText.trim() || busy}
                    onClick={importCurl}
                  >
                    解析并填入
                  </button>
                  {curlFeedback && (
                    <span
                      className={curlFailed ? "wb-error" : "model-note"}
                      role={curlFailed ? "alert" : "status"}
                    >
                      {curlFeedback}
                    </span>
                  )}
                </div>
              </div>
              <div className="model-presets" aria-label="国内模型预设">
                {settings.presets.map(preset => (
                  <button
                    type="button"
                    key={preset.id}
                    className="model-preset"
                    aria-pressed={presetId === preset.id}
                    onClick={() => startNewProvider(preset)}
                  >
                    {preset.name}
                  </button>
                ))}
              </div>
              {selectedPreset?.note && (
                <p className="model-note">{selectedPreset.note}</p>
              )}

              <form className="model-form" onSubmit={event => void save(event)}>
                <label>
                  提供方名称
                  <input
                    autoComplete="off"
                    maxLength={120}
                    value={draft.providerName}
                    onChange={event =>
                      update({ providerName: event.target.value })
                    }
                  />
                </label>
                <label>
                  Base URL
                  <input
                    autoComplete="url"
                    inputMode="url"
                    spellCheck={false}
                    maxLength={2048}
                    placeholder="https://api.example.com/v1"
                    value={draft.baseUrl}
                    onChange={event => {
                      update({ baseUrl: event.target.value });
                      setProbed([]);
                      setProbeMessage("");
                    }}
                  />
                </label>
                <label>
                  API Key
                  <input
                    type="password"
                    autoComplete="new-password"
                    spellCheck={false}
                    maxLength={4096}
                    placeholder={
                      draft.keyConfigured ? "已配置，留空保持不变" : "填写 API Key"
                    }
                    value={draft.apiKey}
                    onChange={event => update({ apiKey: event.target.value })}
                  />
                </label>
                <p className="model-key-hint">{keyHint(draft)}</p>

                {provider && provider.groups.length > 1 && (
                  <label>
                    模型组
                    <select
                      value={draft.originalGroup}
                      onChange={event => chooseGroup(event.target.value)}
                    >
                      {provider.groups.map(group => (
                        <option key={group.name} value={group.name}>
                          {group.name}
                          {group.next_start ? "（默认）" : ""}
                          {group.approved ? "（已批准）" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label>
                  模型组名称
                  <input
                    autoComplete="off"
                    maxLength={120}
                    value={draft.groupName}
                    onChange={event =>
                      update({ groupName: event.target.value })
                    }
                  />
                </label>
                <label className="full-width">
                  模型 ID（每行一个，第一个为主模型）
                  <textarea
                    rows={4}
                    spellCheck={false}
                    placeholder="qwen-plus"
                    value={draft.modelIdsText}
                    onChange={event =>
                      update({ modelIdsText: event.target.value })
                    }
                  />
                </label>
                <p className="model-key-hint full-width">
                  保存会批准该提供方的所有模型组；保存后即可在对话框的模型菜单中按组切换，无需重启。
                </p>
                <details className="full-width model-advanced">
                  <summary>高级：凭据环境变量</summary>
                  <label>
                    凭据环境变量名
                    <input
                      autoComplete="off"
                      spellCheck={false}
                      maxLength={120}
                      placeholder={`留空自动选择（默认 ${KEY_ENV_DEFAULT}）`}
                      value={draft.apiKeyEnv}
                      onChange={event =>
                        update({ apiKeyEnv: event.target.value })
                      }
                    />
                  </label>
                  <p className="model-note">
                    密钥写入工作区 .env。若该变量由服务环境提供，Web 会拒绝覆盖。
                  </p>
                </details>

                <div className="control-row model-actions">
                  <button
                    type="button"
                    disabled={busy || !draft.baseUrl.trim()}
                    onClick={() => void probe()}
                  >
                    获取模型列表
                  </button>
                  {probed.length > 0 && (
                    <select
                      aria-label="从提供方返回的模型中选择"
                      value=""
                      onChange={event => addProbed(event.target.value)}
                    >
                      <option value="">选择模型并填入</option>
                      {probed.map(modelId => (
                        <option key={modelId} value={modelId}>
                          {modelId}
                        </option>
                      ))}
                    </select>
                  )}
                  <button
                    className="primary"
                    type="submit"
                    disabled={!canSave}
                  >
                    {draft.mode === "create"
                      ? "新增并启用"
                      : "保存并启用"}
                  </button>
                </div>
                {probeMessage && (
                  <p className="model-note" role="status">
                    {probeMessage}
                  </p>
                )}
                <p className="model-note">
                  保存只更新选中的提供方和模型组；其他提供方、模型组和 TOML 注释会保留。保存会立即用于新的对话回合；正在执行的回合继续使用原模型。
                </p>
              </form>
            </section>
          )}
        </>
      )}
    </div>
  );
}
