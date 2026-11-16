import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type RefObject,
} from "react";
import { newRequestId } from "../ids";
import { RuntimeError, runtimeApi } from "../runtime-api";
import {
  channelAction,
  ChannelApiError,
  createChannelConnection,
  deleteChannelConnection,
  getChannelCapabilities,
  getChannelOverview,
  getChannelRequestReceipt,
  updateChannelConnection,
  type ChannelActualState,
  type ChannelCapabilities,
  type ChannelConnection,
  type ChannelDisplayState,
  type ChannelOverview,
  type ChannelPlatform,
  type ChannelRequestReceipt,
  type ChannelView,
} from "../channels-api";
import { PageHeader } from "./execution";
import { Icon } from "./Icon";

const PLATFORMS: ChannelPlatform[] = ["wechat", "qq", "telegram", "discord"];
const platformName: Record<ChannelPlatform, string> = {
  wechat: "微信",
  qq: "QQ",
  telegram: "Telegram",
  discord: "Discord",
};
const displayLabel: Record<ChannelDisplayState, string> = {
  unconfigured: "未连接",
  running: "运行中",
  silent: "沉默中",
};
const actualLabel: Record<ChannelActualState, string> = {
  running: "运行中",
  stopped: "已停止",
  connecting: "连接中",
  error: "错误",
};
const REQUEST_KEY = "cairn.web.pending.channels";

type ChannelRoute = {
  platform: ChannelPlatform | "";
  connectionId: string;
  mode: "" | "new" | "edit";
};

type WechatStatus = {
  status: string;
  running: boolean;
  login_pending: boolean;
  verification_required: boolean;
  qr_image: string | null;
  account_saved: boolean;
  enabled: boolean;
};

type PendingEntry = {
  request_id: string;
  identity: string;
  revision?: number;
  at: string;
};

class StaleChannelIntentError extends Error {
  constructor(
    message: string,
    readonly reconciled: boolean,
    readonly revision: number | null = null,
  ) {
    super(message);
    this.name = "StaleChannelIntentError";
  }
}

function readRoute(): ChannelRoute {
  const params = new URLSearchParams(location.search);
  const requested = params.get("platform") ?? "";
  const platform = PLATFORMS.includes(requested as ChannelPlatform)
    ? (requested as ChannelPlatform)
    : "";
  const connectionId = platform ? (params.get("connection_id") ?? "") : "";
  const rawMode = params.get("mode");
  const mode: ChannelRoute["mode"] =
    rawMode === "new" && platform
      ? "new"
      : rawMode === "edit" && connectionId
        ? "edit"
        : "";
  return { platform, connectionId, mode };
}

function writeRoute(route: ChannelRoute, mode: "push" | "replace") {
  const params = new URLSearchParams();
  if (route.platform) params.set("platform", route.platform);
  if (route.connectionId) params.set("connection_id", route.connectionId);
  if (route.mode) params.set("mode", route.mode);
  const url = location.pathname + (params.size ? `?${params}` : "");
  if (mode === "replace") window.history.replaceState({}, "", url);
  else window.history.pushState({}, "", url);
}

// Private browsing or blocked site data can make localStorage unavailable.
// Keeping the same map in memory still lets this page retry an uncertain
// request with its original identity.
let memoryPending: Record<string, PendingEntry> = {};

function readPending(): Record<string, PendingEntry> {
  try {
    const raw = localStorage.getItem(REQUEST_KEY);
    if (raw === null) return { ...memoryPending };
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { ...memoryPending };
    }
    const entries = Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>)
        .map(([scope, value]) => {
          if (!value || typeof value !== "object" || Array.isArray(value)) {
            return null;
          }
          const entry = value as Record<string, unknown>;
          if (typeof entry.request_id !== "string" || !entry.request_id) {
            return null;
          }
          return [
            scope,
            {
              request_id: entry.request_id,
              identity:
                typeof entry.identity === "string" ? entry.identity : "",
              ...(typeof entry.revision === "number"
                ? { revision: entry.revision }
                : {}),
              at: typeof entry.at === "string" ? entry.at : "",
            } satisfies PendingEntry,
          ] as const;
        })
        .filter((item): item is readonly [string, PendingEntry] => item !== null),
    );
    memoryPending = entries;
    return entries;
  } catch {
    return { ...memoryPending };
  }
}

function writePending(pending: Record<string, PendingEntry>) {
  const bounded = Object.fromEntries(Object.entries(pending).slice(-20));
  memoryPending = bounded;
  try {
    localStorage.setItem(REQUEST_KEY, JSON.stringify(bounded));
  } catch {
    /* memoryPending keeps the retry identity for this page */
  }
}

async function intentFingerprint(intent: Record<string, unknown>) {
  const text = JSON.stringify(intent);
  if (globalThis.crypto?.subtle) {
    try {
      const digest = await globalThis.crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(text),
      );
      return (
        "sha256:" +
        Array.from(new Uint8Array(digest), value =>
          value.toString(16).padStart(2, "0"),
        ).join("")
      );
    } catch {
      /* fall through to the deterministic local digest */
    }
  }
  // The secret values themselves must never be stored. This fallback only
  // protects an equality check when Web Crypto is unavailable on plain HTTP.
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `fnv1a:${(hash >>> 0).toString(16)}:${text.length}`;
}

function rememberRequestId(
  scope: string,
  identity: string,
  revision: number | null,
) {
  const pending = readPending();
  const requestId = newRequestId();
  delete pending[scope];
  pending[scope] = {
    request_id: requestId,
    identity,
    ...(revision === null ? {} : { revision }),
    at: new Date().toISOString(),
  };
  writePending(pending);
  return { requestId, expectedRevision: revision };
}

function receiptRevision(receipt: ChannelRequestReceipt) {
  const connection = receipt.result?.connection;
  return connection &&
    typeof connection === "object" &&
    typeof (connection as Record<string, unknown>).revision === "number"
    ? Number((connection as Record<string, unknown>).revision)
    : null;
}

async function requestIdFor(
  scope: string,
  identity: string,
  revision: number | null,
): Promise<{ requestId: string; expectedRevision: number | null }> {
  const pending = readPending();
  const current = pending[scope];
  if (!current) return rememberRequestId(scope, identity, revision);
  if (current.identity && current.identity === identity) {
    // Rebuild the original payload, including its original revision, so the
    // Server sees exactly the same request instead of rejecting a new intent.
    return {
      requestId: current.request_id,
      expectedRevision: current.revision ?? revision,
    };
  }
  let receipt: ChannelRequestReceipt;
  try {
    receipt = await getChannelRequestReceipt(current.request_id);
  } catch (reason) {
    if (
      reason instanceof ChannelApiError &&
      reason.status === 404 &&
      reason.code === "channel_request_not_found"
    ) {
      // No durable receipt means the old intent never reached the Server.
      delete pending[scope];
      writePending(pending);
      return rememberRequestId(scope, identity, revision);
    }
    throw new StaleChannelIntentError(
      "上一次提交的结果仍未知，请稍后重试，或先刷新核对连接状态。",
      false,
    );
  }
  if (receipt.state === "pending") {
    throw new StaleChannelIntentError(
      "上一次提交的结果仍在确认中；核对完成前不会用新内容重放旧请求。",
      false,
    );
  }
  if (receipt.state !== "completed") {
    throw new StaleChannelIntentError(
      "上一次提交的结果无法确认，请先刷新核对连接状态。",
      false,
    );
  }
  delete pending[scope];
  writePending(pending);
  throw new StaleChannelIntentError(
    "上一次提交已完成；连接状态正在刷新，请核对后重新提交本次修改。",
    true,
    receiptRevision(receipt),
  );
}

function clearRequestId(key: string, requestId: string) {
  const pending = readPending();
  if (pending[key]?.request_id !== requestId) return;
  delete pending[key];
  writePending(pending);
}

function shouldKeepRequestId(reason: unknown) {
  return (
    reason instanceof RuntimeError &&
    (reason.status === 0 ||
      [500, 502, 503, 504].includes(reason.status) ||
      (reason instanceof ChannelApiError &&
        reason.code === "channel_operation_uncertain"))
  );
}

function messageOf(reason: unknown) {
  return reason instanceof Error ? reason.message : String(reason);
}

function dateLabel(value: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime())
    ? parsed.toLocaleString("zh-CN", { hour12: false })
    : value;
}

function parseSenders(value: string) {
  return Array.from(
    new Set(
      value
        .split(/[\n,，]/)
        .map(item => item.trim())
        .filter(Boolean),
    ),
  );
}

function useChannelAnimation(
  ref: RefObject<HTMLDivElement | null>,
  animationKey: string,
) {
  useEffect(() => {
    if (!animationKey) return;
    let cancelled = false;
    let media: { revert: () => void } | undefined;
    void import("gsap").then(({ gsap }) => {
      if (cancelled || !ref.current) return;
      const context = gsap.matchMedia();
      media = context;
      context.add("(prefers-reduced-motion: no-preference)", () => {
        const items = ref.current?.querySelectorAll("[data-channel-animate]");
        if (items?.length) {
          gsap.from(items, {
            y: 9,
            autoAlpha: 0,
            duration: 0.26,
            stagger: 0.035,
            ease: "power2.out",
            clearProps: "transform,opacity,visibility",
          });
        }
      });
    });
    return () => {
      cancelled = true;
      media?.revert();
    };
  }, [ref, animationKey]);
}

function useVisiblePoll<T>(
  enabled: boolean,
  restart: number,
  epoch: { current: number },
  load: (signal: AbortSignal) => Promise<T>,
  onValue: (value: T) => void,
  onError: (reason: unknown) => void,
) {
  const callbacks = useRef({ load, onValue, onError });
  callbacks.current = { load, onValue, onError };
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let controller: AbortController | null = null;
    const tick = async () => {
      if (stopped || controller) return;
      const requestEpoch = epoch.current;
      const current = new AbortController();
      controller = current;
      try {
        const value = await callbacks.current.load(current.signal);
        if (
          stopped ||
          current.signal.aborted ||
          epoch.current !== requestEpoch
        ) {
          return;
        }
        callbacks.current.onValue(value);
      } catch (reason) {
        if (
          !stopped &&
          !current.signal.aborted &&
          epoch.current === requestEpoch
        ) {
          callbacks.current.onError(reason);
        }
      } finally {
        if (controller === current) controller = null;
      }
    };
    void tick();
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") void tick();
    }, 4000);
    return () => {
      stopped = true;
      clearInterval(timer);
      controller?.abort();
      controller = null;
    };
  }, [enabled, restart, epoch]);
}

function PlatformMark({ platform }: { platform: ChannelPlatform }) {
  return (
    <span className="channel-mark" data-platform={platform} aria-hidden="true">
      {platform === "wechat" ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M9.5 4.5c3 0 5.5 1.9 5.5 4.3s-2.5 4.3-5.5 4.3c-.6 0-1.2-.1-1.8-.2L5.5 14l.7-2.2c-1.3-.8-2.2-1.8-2.2-3C4 6.4 6.5 4.5 9.5 4.5Z" />
          <path d="M15 10.5c2.6 0 4.8 1.6 4.8 3.8 0 1-.6 1.9-1.6 2.6l.5 1.8-2.1-1c-.5.1-1 .2-1.6.2-2.6 0-4.8-1.6-4.8-3.8" />
        </svg>
      ) : platform === "qq" ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M12 3.5c2.5 0 4.2 1.8 4.2 4.3 0 1-.2 1.7-.5 2.4 1.5 1.2 2.6 3.3 2.6 5.4 0 2.5-2.8 4.4-6.3 4.4s-6.3-1.9-6.3-4.4c0-2.1 1.1-4.2 2.6-5.4-.3-.7-.5-1.4-.5-2.4 0-2.5 1.7-4.3 4.2-4.3Z" />
          <circle cx="10" cy="8" r="0.8" fill="currentColor" stroke="none" />
          <circle cx="14" cy="8" r="0.8" fill="currentColor" stroke="none" />
          <path d="m10.5 11 1.5-1 1.5 1-1.5 1.2-1.5-1.2Z" fill="currentColor" stroke="none" />
        </svg>
      ) : platform === "telegram" ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M20.5 4.5 3.9 10.2c-.8.3-.8 1.4.1 1.6l4.1 1.1 1.6 4.6c.2.7 1.1.9 1.6.3l2.2-2.5 4.3 3.1c.6.4 1.4.1 1.5-.6l2.4-12.3c.1-.8-.5-1.3-1.2-1Z" />
          <path d="m8.1 12.9 9.3-6.2-7.2 7.3-.3 3.2" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M7.2 5.5h9.6c2.4 0 4.2 1.9 4.2 4.3v5.1c0 1.6-1.3 2.9-2.8 2.9-.9 0-1.7-.4-2.2-1.1l-1.2-1.6H9.2L8 16.7c-.5.7-1.3 1.1-2.2 1.1-1.5 0-2.8-1.3-2.8-2.9v-5.1c0-2.4 1.8-4.3 4.2-4.3Z" />
          <circle cx="9" cy="10.5" r="1.2" fill="currentColor" stroke="none" />
          <circle cx="15" cy="10.5" r="1.2" fill="currentColor" stroke="none" />
        </svg>
      )}
    </span>
  );
}

function StatusPill({
  state,
  kind = "display",
}: {
  state: ChannelDisplayState | ChannelActualState;
  kind?: "display" | "actual";
}) {
  const label =
    kind === "display"
      ? displayLabel[state as ChannelDisplayState]
      : actualLabel[state as ChannelActualState];
  return (
    <span className="channel-state" data-state={state}>
      <i aria-hidden="true" />
      {label}
    </span>
  );
}

function WechatLegacyPanel({
  status,
  error,
}: {
  status: WechatStatus | null;
  error: string;
}) {
  return (
    <section className="wb-card channels-legacy" aria-label="微信宿主托管" data-channel-animate>
      <div>
        <h2>宿主托管的微信</h2>
        <p className="wb-muted">
          微信沿用 Server 现有的单一登录会话，作为 legacy-wechat 展示；这里不会创建第二个
          微信 Controller，也不会删除旧登录状态。扫码、验证码、账号与断开操作仍在设置页。
        </p>
      </div>
      {status ? (
        <div className="channels-legacy-state">
          <p className="channels-legacy-status" role="status">
            {status.status}
          </p>
          <p className="wb-muted">
            {status.enabled ? "宿主已启用" : "宿主已停用"}
            {" · "}
            {status.running ? "后台接收中" : "当前未运行"}
            {" · "}
            {status.account_saved ? "已保存登录账号" : "尚未保存登录账号"}
          </p>
          {status.login_pending ? (
            <p
              className="channels-note"
              role="status"
              data-wechat-notice={
                status.verification_required ? "verification" : "scan"
              }
            >
              {status.verification_required
                ? "微信要求输入登录验证码；请在设置 → 微信提交验证码。"
                : "等待扫码登录；请使用上方二维码，或前往设置 → 微信继续。"}
            </p>
          ) : (
            status.verification_required && (
              <p
                className="channels-note"
                role="alert"
                data-wechat-notice="verification"
              >
                微信要求输入登录验证码；请在设置 → 微信完成验证。
              </p>
            )
          )}
        </div>
      ) : (
        <p className="wb-muted" role="status">
          {error || "正在读取微信登录状态…"}
        </p>
      )}
      {status?.qr_image && (
        <figure className="channels-qr">
          <img src={status.qr_image} alt="微信登录二维码" width="180" height="180" />
          <figcaption>使用微信扫码登录（与设置页共用同一登录流程）</figcaption>
        </figure>
      )}
      <a className="channels-settings-link" href="/settings.html?section=wechat">
        <Icon name="settings" size={14} />
        打开微信设置
      </a>
    </section>
  );
}

function ConnectionForm({
  platform,
  capability,
  initial,
  onCancel,
  onSaved,
  onDirty,
  onReconciled,
}: {
  platform: ChannelPlatform;
  capability: ChannelCapabilities;
  initial: ChannelConnection | null;
  onCancel: () => void;
  onSaved: (view: ChannelView, created: boolean) => void;
  onDirty: () => void;
  onReconciled: () => void;
}) {
  const editing = initial !== null;
  const [displayName, setDisplayName] = useState(initial?.display_name ?? "");
  const [enabled, setEnabled] = useState(initial?.enabled ?? true);
  const [owner, setOwner] = useState(initial?.owner_identity ?? "");
  const [senders, setSenders] = useState(
    (initial?.allowed_senders ?? []).join("\n"),
  );
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [errorKind, setErrorKind] = useState<
    "validation" | "transport" | "stale-intent" | ""
  >("");
  const [busy, setBusy] = useState(false);
  const [reconciledRevision, setReconciledRevision] = useState(0);

  const fields = capability.credential_schema;
  const filled = Object.fromEntries(
    fields
      .map(field => [field.key, credentials[field.key]?.trim() ?? ""])
      .filter(([, value]) => value !== ""),
  );
  const replacing = Object.keys(filled).length > 0;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    const name = displayName.trim();
    if (!name) {
      setError("请填写连接名称。");
      setErrorKind("validation");
      return;
    }
    if (replacing) {
      const missing = fields.filter(
        field => field.required && !filled[field.key],
      );
      if (missing.length) {
        setError(`替换凭据需要填写：${missing.map(field => field.label).join("、")}`);
        setErrorKind("validation");
        return;
      }
    } else if (!editing) {
      const missing = fields.filter(field => field.required);
      if (missing.length) {
        setError(`请填写凭据：${missing.map(field => field.label).join("、")}`);
        setErrorKind("validation");
        return;
      }
    }
    const changes = {
      display_name: name,
      enabled,
      owner_identity: owner.trim() || null,
      allowed_senders: parseSenders(senders),
    };
    const scope = editing
      ? `update:${initial.connection_id}`
      : `create:${platform}`;
    const intent = editing
      ? {
          operation: "update",
          connection_id: initial.connection_id,
          ...changes,
          credentials: replacing ? filled : null,
          replace_credentials: replacing,
        }
      : {
          operation: "create",
          platform,
          ...changes,
          credentials: replacing ? filled : null,
        };
    const fingerprint = await intentFingerprint(intent);
    const baseRevision = editing
      ? Math.max(initial.revision, reconciledRevision)
      : null;
    setBusy(true);
    setError("");
    setErrorKind("");
    let requestId = "";
    try {
      const claim = await requestIdFor(scope, fingerprint, baseRevision);
      requestId = claim.requestId;
      const view = editing
        ? await updateChannelConnection(initial.connection_id, {
            expected_revision: claim.expectedRevision ?? initial.revision,
            request_id: requestId,
            ...changes,
            ...(replacing
              ? { credentials: filled, replace_credentials: true }
              : {}),
          })
        : await createChannelConnection(platform, {
            request_id: requestId,
            ...changes,
            ...(replacing ? { credentials: filled } : {}),
          });
      clearRequestId(scope, requestId);
      setReconciledRevision(0);
      onSaved(view, !editing);
    } catch (reason) {
      if (reason instanceof StaleChannelIntentError) {
        setError(reason.message);
        setErrorKind("stale-intent");
        if (reason.reconciled) {
          setReconciledRevision(current =>
            Math.max(current, reason.revision ?? 0),
          );
          onReconciled();
        }
        return;
      }
      if (!shouldKeepRequestId(reason) && requestId) {
        clearRequestId(scope, requestId);
      }
      setError(messageOf(reason));
      setErrorKind("transport");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="channels-form" onSubmit={submit} data-channel-animate>
      <label>
        连接名称
        <input
          required
          maxLength={120}
          value={displayName}
          onChange={event => {
            setDisplayName(event.target.value);
            onDirty();
          }}
          placeholder={`例如：我的 ${platformName[platform]}`}
        />
      </label>
      <label className="channels-check">
        <input
          type="checkbox"
          checked={enabled}
          onChange={event => {
            setEnabled(event.target.checked);
            onDirty();
          }}
        />
        保存后允许该连接运行
      </label>
      <label>
        宿主所有者（可选）
        <input
          maxLength={200}
          value={owner}
          onChange={event => {
            setOwner(event.target.value);
            onDirty();
          }}
          placeholder="平台侧的用户或机器人身份"
        />
      </label>
      <label>
        允许的发送者（可选，每行一个）
        <textarea
          rows={3}
          value={senders}
          onChange={event => {
            setSenders(event.target.value);
            onDirty();
          }}
          placeholder="留空表示仅宿主所有者"
        />
      </label>
      {fields.length > 0 && (
        <fieldset className="channels-credentials">
          <legend>{editing ? "替换凭据" : "凭据"}</legend>
          <p className="wb-muted">
            凭据只写入，不会回显。
            {editing
              ? "留空表示保留现有凭据；填写任意一项即替换全部凭据。"
              : "所有必填项仅在此次提交时发送。"}
          </p>
          {fields.map(field => (
            <label key={field.key}>
              {field.label}
              {field.required ? "" : "（可选）"}
              <input
                type={field.secret ? "password" : "text"}
                autoComplete="new-password"
                value={credentials[field.key] ?? ""}
                onChange={event => {
                  setCredentials(current => ({
                    ...current,
                    [field.key]: event.target.value,
                  }));
                  onDirty();
                }}
                placeholder={
                  editing
                    ? "留空表示保留"
                    : field.placeholder ?? (field.secret ? "仅写入" : "")
                }
              />
            </label>
          ))}
        </fieldset>
      )}
      {!capability.adapter_supported && (
        <p className="channels-note" role="note">
          Server 尚未实现该平台的 Adapter。配置可以保存，但不会显示为运行中，也不能启动。
        </p>
      )}
      {error && (
        <p
          className="channels-error"
          role="alert"
          data-channel-error={errorKind || undefined}
        >
          {error}
        </p>
      )}
      <div className="channels-form-actions">
        <button className="primary" disabled={busy}>
          {busy ? "保存中…" : editing ? "保存修改" : "保存连接"}
        </button>
        <button type="button" disabled={busy} onClick={onCancel}>
          取消
        </button>
      </div>
    </form>
  );
}

export function Channels() {
  const [route, setRoute] = useState<ChannelRoute>(readRoute);
  const [overview, setOverview] = useState<ChannelOverview | null>(null);
  const [capabilities, setCapabilities] = useState<
    Record<ChannelPlatform, ChannelCapabilities>
  >({} as Record<ChannelPlatform, ChannelCapabilities>);
  const [overviewError, setOverviewError] = useState("");
  const [capabilityError, setCapabilityError] = useState("");
  const [revision, setRevision] = useState(0);
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [wechat, setWechat] = useState<WechatStatus | null>(null);
  const [wechatError, setWechatError] = useState("");
  const pageRef = useRef<HTMLDivElement>(null);
  const pollEpoch = useRef(0);

  const navigate = useCallback((next: ChannelRoute, mode: "push" | "replace") => {
    writeRoute(next, mode);
    setRoute(next);
    setActionError("");
    setActionNotice("");
    setConfirmDelete(false);
    document.querySelector(".wb-main")?.scrollTo(0, 0);
  }, []);

  useEffect(() => {
    const update = () => {
      setRoute(readRoute());
      setActionError("");
      setActionNotice("");
      setConfirmDelete(false);
    };
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  useVisiblePoll(
    true,
    revision,
    pollEpoch,
    getChannelOverview,
    value => {
      setOverview(value);
      setOverviewError("");
    },
    reason => setOverviewError(messageOf(reason)),
  );

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    getChannelCapabilities(controller.signal)
      .then(value => {
        if (cancelled) return;
        setCapabilities(
          Object.fromEntries(
            value.platforms.map(row => [row.platform, row]),
          ) as Record<ChannelPlatform, ChannelCapabilities>,
        );
      })
      .catch(reason => {
        if (!cancelled && !controller.signal.aborted) {
          setCapabilityError(messageOf(reason));
        }
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  useEffect(() => {
    if (route.platform !== "wechat") {
      setWechat(null);
      setWechatError("");
    }
  }, [route.platform]);

  useVisiblePoll(
    route.platform === "wechat",
    revision,
    pollEpoch,
    async signal => {
      const value = await runtimeApi<{ wechat?: WechatStatus }>(
        "management?section=wechat",
        undefined,
        signal,
      );
      if (!value.wechat || typeof value.wechat.status !== "string") {
        throw new Error("Server 未返回微信登录状态。");
      }
      return value.wechat;
    },
    value => {
      setWechat(value);
      setWechatError("");
    },
    reason => setWechatError(messageOf(reason)),
  );

  const platform = route.platform || null;
  const summary = platform
    ? overview?.platforms.find(row => row.platform === platform)
    : undefined;
  const capability = platform ? capabilities[platform] : undefined;
  const platformViews = platform
    ? (overview?.connections ?? []).filter(
        view => view.connection.platform === platform,
      )
    : [];
  const current = route.connectionId
    ? (overview?.connections ?? []).find(
        view => view.connection.connection_id === route.connectionId,
      ) ?? null
    : null;
  const loading = !overview && !overviewError;
  const animationKey = route.mode
    ? `${route.platform}:${route.mode}:${route.connectionId}`
    : route.connectionId
      ? `connection:${route.connectionId}:${current?.runtime.actual_state ?? ""}`
      : platform
        ? `platform:${platform}:${platformViews
            .map(view => `${view.connection.connection_id}:${view.runtime.actual_state}`)
            .join(",")}`
        : `overview:${(overview?.platforms ?? [])
            .map(row => `${row.platform}:${row.display_state}:${row.connections}:${row.running}`)
            .join(",")}`;

  useChannelAnimation(pageRef, animationKey);

  function handleStaleIntent(reason: StaleChannelIntentError) {
    setActionError(reason.message);
    if (!reason.reconciled) return;
    pollEpoch.current += 1;
    setRevision(value => value + 1);
  }

  async function runAction(action: "start" | "stop" | "reconnect") {
    if (!current || busy) return;
    const connectionId = current.connection.connection_id;
    const key = `action:${action}:${connectionId}`;
    const identity = `action:${action}:${connectionId}`;
    setBusy(action);
    setActionError("");
    setActionNotice("");
    let requestId = "";
    try {
      requestId = (await requestIdFor(key, identity, null)).requestId;
      const view = await channelAction(connectionId, action, requestId);
      clearRequestId(key, requestId);
      pollEpoch.current += 1;
      setActionNotice(
        `${action === "start" ? "已提交启动" : action === "stop" ? "已提交停止" : "已提交重连"}；状态以 Server 为准。`,
      );
      setOverview(state =>
        state
          ? {
              ...state,
              connections: state.connections.map(row =>
                row.connection.connection_id === view.connection.connection_id
                  ? view
                  : row,
              ),
            }
          : state,
      );
    } catch (reason) {
      if (reason instanceof StaleChannelIntentError) {
        handleStaleIntent(reason);
        return;
      }
      if (!shouldKeepRequestId(reason) && requestId) {
        clearRequestId(key, requestId);
      }
      setActionError(messageOf(reason));
    } finally {
      setBusy("");
    }
  }

  async function removeConnection() {
    if (!current || busy) return;
    const connectionId = current.connection.connection_id;
    const revision = current.connection.revision;
    const key = `delete:${connectionId}:${revision}`;
    const identity = `delete:${connectionId}:${revision}`;
    setBusy("delete");
    setActionError("");
    let requestId = "";
    try {
      requestId = (await requestIdFor(key, identity, null)).requestId;
      await deleteChannelConnection(connectionId, {
        expected_revision: revision,
        request_id: requestId,
        confirm: true,
      });
      clearRequestId(key, requestId);
      setConfirmDelete(false);
      pollEpoch.current += 1;
      setRevision(value => value + 1);
      navigate(
        { platform: current.connection.platform, connectionId: "", mode: "" },
        "replace",
      );
    } catch (reason) {
      if (reason instanceof StaleChannelIntentError) {
        handleStaleIntent(reason);
        return;
      }
      if (!shouldKeepRequestId(reason) && requestId) {
        clearRequestId(key, requestId);
      }
      setActionError(messageOf(reason));
    } finally {
      setBusy("");
    }
  }

  function openNew() {
    if (!platform) return;
    navigate({ platform, connectionId: "", mode: "new" }, "push");
  }

  function openEdit() {
    if (!current) return;
    navigate(
      {
        platform: current.connection.platform,
        connectionId: current.connection.connection_id,
        mode: "edit",
      },
      "push",
    );
  }

  const headerActions = (
    <button disabled={loading} onClick={() => setRevision(value => value + 1)}>
      刷新
    </button>
  );

  return (
    <div className="wb-page channels-page" ref={pageRef}>
      {!platform && (
        <>
          <PageHeader
            icon="server"
            title="连接"
            description="管理微信、QQ、Telegram 与 Discord 的连接和运行状态"
            actions={headerActions}
          />
          {overviewError && (
            <p className="channels-error" role="alert">
              {overviewError}
            </p>
          )}
          {loading ? (
            <p className="wb-muted" role="status">
              正在读取连接状态…
            </p>
          ) : (
            <div className="channels-overview">
              {PLATFORMS.map(candidate => {
                const row = overview?.platforms.find(
                  item => item.platform === candidate,
                );
                const state = row?.display_state ?? "unconfigured";
                return (
                  <button
                    className="wb-card channels-platform"
                    data-platform={candidate}
                    data-channel-animate
                    key={candidate}
                    onClick={() =>
                      navigate(
                        { platform: candidate, connectionId: "", mode: "" },
                        "push",
                      )
                    }
                  >
                    <PlatformMark platform={candidate} />
                    <span className="channels-platform-copy">
                      <strong>{platformName[candidate]}</strong>
                      <span className="wb-muted">
                        已配置 {row?.connections ?? 0} · 运行中 {row?.running ?? 0}
                      </span>
                    </span>
                    <StatusPill state={state} />
                  </button>
                );
              })}
            </div>
          )}
          <p className="wb-muted channels-footnote">
            四个平台固定展示；状态、配置数和运行数均取自 Server 的真实连接状态。
          </p>
        </>
      )}

      {platform && !route.mode && !route.connectionId && (
        <>
          <PageHeader
            icon="server"
            title={`${platformName[platform]}连接`}
            description={
              summary
                ? `已配置 ${summary.connections} · 运行中 ${summary.running}`
                : undefined
            }
            actions={
              <>
                <button
                  onClick={() =>
                    navigate(
                      { platform: "", connectionId: "", mode: "" },
                      "push",
                    )
                  }
                >
                  返回总览
                </button>
                {capability?.supported_actions.includes("create") && (
                  <button className="primary" onClick={openNew}>
                    <Icon name="plus" size={15} />
                    新建连接
                  </button>
                )}
              </>
            }
          />
          {overviewError && (
            <p className="channels-error" role="alert">
              {overviewError}
            </p>
          )}
          {capabilityError && !capability && (
            <p className="channels-error" role="alert">
              {capabilityError}
            </p>
          )}
          {capability && !capability.adapter_supported && (
            <p className="channels-note" role="note">
              Server 尚未实现该平台的 Adapter。连接可以保存和编辑，但不会显示为运行中，
              启动操作也不可用。
            </p>
          )}
          {platform === "wechat" && (
            <WechatLegacyPanel status={wechat} error={wechatError} />
          )}
          {loading ? (
            <p className="wb-muted" role="status">
              正在读取连接列表…
            </p>
          ) : platformViews.length ? (
            <div className="channels-list">
              {platformViews.map(view => (
                <button
                  className="wb-card channels-connection"
                  data-channel-animate
                  key={view.connection.connection_id}
                  onClick={() =>
                    navigate(
                      {
                        platform,
                        connectionId: view.connection.connection_id,
                        mode: "",
                      },
                      "push",
                    )
                  }
                >
                  <span className="channels-connection-main">
                    <strong>
                      {view.connection.display_name}
                      {view.connection.legacy && (
                        <em className="channels-legacy-badge">宿主托管</em>
                      )}
                    </strong>
                    <span className="wb-muted">
                      {view.connection.connection_id} · v{view.connection.revision}
                    </span>
                    {view.runtime.last_error && (
                      <span className="channels-error-line" role="note">
                        最近错误：{view.runtime.last_error}
                      </span>
                    )}
                  </span>
                  <StatusPill
                    state={view.runtime.actual_state}
                    kind="actual"
                  />
                </button>
              ))}
            </div>
          ) : (
            <div className="wb-card channels-empty">
              <p>
                {platform === "wechat"
                  ? "微信尚未登录。请在设置页完成扫码登录；该连接会作为 legacy-wechat 自动出现。"
                  : `还没有 ${platformName[platform]} 连接。`}
              </p>
              {capability?.supported_actions.includes("create") && (
                <button className="primary" onClick={openNew}>
                  <Icon name="plus" size={15} />
                  新建连接
                </button>
              )}
            </div>
          )}
        </>
      )}

      {platform && route.mode && capability && (
        <>
          <PageHeader
            icon="server"
            title={route.mode === "edit" ? "编辑连接" : "新建连接"}
            description={`${platformName[platform]} · ${
              route.mode === "edit" ? current?.connection.display_name ?? "" : "凭据仅写入"
            }`}
            actions={
              <button
                onClick={() =>
                  navigate(
                    {
                      platform,
                      connectionId: route.mode === "edit" ? route.connectionId : "",
                      mode: "",
                    },
                    "push",
                  )
                }
              >
                返回
              </button>
            }
          />
          {route.mode === "edit" && !current ? (
            <p className="channels-error" role="alert">
              该连接不存在或已被删除。
            </p>
          ) : (
            <ConnectionForm
              key={`${platform}:${route.connectionId}:${route.mode}`}
              platform={platform}
              capability={capability}
              initial={route.mode === "edit" ? current?.connection ?? null : null}
              onCancel={() =>
                navigate(
                  {
                    platform,
                    connectionId: route.mode === "edit" ? route.connectionId : "",
                    mode: "",
                  },
                  "push",
                )
              }
              onDirty={() => {
                setActionError("");
                setActionNotice("");
              }}
              onReconciled={() => {
                pollEpoch.current += 1;
                setRevision(value => value + 1);
              }}
              onSaved={(view, created) => {
                pollEpoch.current += 1;
                setRevision(value => value + 1);
                navigate(
                  {
                    platform: view.connection.platform,
                    connectionId: view.connection.connection_id,
                    mode: "",
                  },
                  "replace",
                );
                setActionNotice(created ? "连接已保存。" : "连接修改已保存。");
              }}
            />
          )}
        </>
      )}

      {platform && route.mode && !capability && (
        <p className="wb-muted" role="status">
          {capabilityError || "正在读取平台能力…"}
        </p>
      )}

      {platform && !route.mode && current && (
        <>
          <PageHeader
            icon="server"
            title={current.connection.display_name}
            description={`${platformName[platform]} · ${current.connection.connection_id}`}
            actions={
              <button
                onClick={() =>
                  navigate(
                    { platform, connectionId: "", mode: "" },
                    "push",
                  )
                }
              >
                返回列表
              </button>
            }
          />
          {current.connection.legacy && (
            <p className="channels-note" role="note">
              这是宿主托管的 legacy-wechat 连接，由现有微信登录会话提供；不能通过普通
              连接 CRUD 编辑或删除，旧登录状态也不会在这里被移除。
            </p>
          )}
          {overviewError && (
            <p className="channels-error" role="alert">
              {overviewError}
            </p>
          )}
          <section className="wb-card channels-detail" aria-label="连接详情" data-channel-animate>
            <div className="channels-detail-head">
              <PlatformMark platform={platform} />
              <div>
                <strong>{current.connection.display_name}</strong>
                <span className="wb-muted">
                  {platformName[platform]} · v{current.connection.revision}
                </span>
              </div>
              <StatusPill state={current.runtime.actual_state} kind="actual" />
            </div>
            <dl className="channels-facts">
              <div>
                <dt>当前状态</dt>
                <dd data-state={current.runtime.actual_state}>
                  {actualLabel[current.runtime.actual_state]}
                </dd>
              </div>
              <div>
                <dt>期望状态</dt>
                <dd>
                  {current.connection.desired_state === "running" ? "运行" : "停止"}
                </dd>
              </div>
              <div>
                <dt>已启用</dt>
                <dd>{current.connection.enabled ? "是" : "否"}</dd>
              </div>
              <div>
                <dt>上次连接时间</dt>
                <dd>{dateLabel(current.runtime.last_connected_at)}</dd>
              </div>
              <div>
                <dt>上次心跳</dt>
                <dd>{dateLabel(current.runtime.last_heartbeat_at)}</dd>
              </div>
              <div>
                <dt>下次重试</dt>
                <dd>{dateLabel(current.runtime.next_retry_at)}</dd>
              </div>
              <div className="channels-fact-wide">
                <dt>最近错误</dt>
                <dd>{current.runtime.last_error ?? "无"}</dd>
              </div>
            </dl>
            <div className="channels-detail-actions">
              <button
                className={capability?.adapter_supported ? "primary" : undefined}
                disabled={
                  Boolean(busy) ||
                  !capability?.supported_actions.includes("start") ||
                  current.runtime.actual_state === "running"
                }
                title={
                  capability?.supported_actions.includes("start")
                    ? undefined
                    : "Server 尚未实现该平台 Adapter"
                }
                onClick={() => void runAction("start")}
              >
                {busy === "start" ? "启动中…" : "启动"}
              </button>
              <button
                disabled={
                  Boolean(busy) ||
                  !capability?.supported_actions.includes("stop") ||
                  current.runtime.actual_state === "stopped"
                }
                onClick={() => void runAction("stop")}
              >
                {busy === "stop" ? "停止中…" : "停止"}
              </button>
              <button
                disabled={
                  Boolean(busy) ||
                  !capability?.supported_actions.includes("reconnect")
                }
                onClick={() => void runAction("reconnect")}
              >
                {busy === "reconnect" ? "重连中…" : "重连"}
              </button>
              {!current.connection.legacy &&
                capability?.supported_actions.includes("update") && (
                  <button disabled={Boolean(busy)} onClick={openEdit}>
                    编辑
                  </button>
                )}
              {!current.connection.legacy &&
                capability?.supported_actions.includes("delete") && (
                  <button
                    className="channels-danger"
                    disabled={Boolean(busy)}
                    onClick={() => setConfirmDelete(true)}
                  >
                    删除
                  </button>
                )}
            </div>
            {!capability?.adapter_supported && (
              <p className="channels-note" role="note">
                配置已保存；Server 没有该平台的真实 Adapter，因此不会启动，也不会调用
                Fake Adapter。
              </p>
            )}
            {current.connection.legacy && (
              <p className="channels-note" role="note">
                微信扫码、验证码与账号状态请使用
                <a href="/settings.html?section=wechat">设置 → 微信</a>；此处只复用宿主
                已有的启动、停止和重连能力。
              </p>
            )}
            {actionError && (
              <p className="channels-error" role="alert">
                {actionError}
              </p>
            )}
            {actionNotice && (
              <p className="wb-muted" role="status">
                {actionNotice}
              </p>
            )}
            {confirmDelete && (
              <div className="channels-confirm" role="alertdialog" aria-label="确认删除连接">
                <p>
                  删除「{current.connection.display_name}」后无法恢复，审计记录会保留。
                  确定删除？
                </p>
                <div className="channels-detail-actions">
                  <button
                    className="channels-danger primary"
                    disabled={Boolean(busy)}
                    onClick={() => void removeConnection()}
                  >
                    {busy === "delete" ? "删除中…" : "确认删除"}
                  </button>
                  <button disabled={Boolean(busy)} onClick={() => setConfirmDelete(false)}>
                    取消
                  </button>
                </div>
              </div>
            )}
          </section>
        </>
      )}

      {platform && !route.mode && route.connectionId && !current && (
        <p className={loading ? "wb-muted" : "channels-error"} role={loading ? "status" : "alert"}>
          {loading ? "正在读取连接…" : overviewError || "该连接不存在或已被删除。"}
        </p>
      )}
    </div>
  );
}
