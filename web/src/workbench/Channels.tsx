import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
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

const PLATFORMS: ChannelPlatform[] = [
  "wechat",
  "feishu",
  "wecom",
  "qq",
  "telegram",
  "discord",
];
const platformName: Record<ChannelPlatform, string> = {
  wechat: "微信",
  feishu: "飞书",
  wecom: "企业微信",
  qq: "QQ",
  telegram: "Telegram",
  discord: "Discord",
};
const platformBlurb: Record<ChannelPlatform, string> = {
  wechat: "复用宿主登录会话，在私聊与群聊中收发消息。",
  feishu: "用企业自建应用接入飞书，通过长连接接收私聊消息。",
  wecom: "企业微信接入尚未集成，暂时只展示连接配置。",
  qq: "配置 QQ 机器人连接，集中管理运行状态。",
  telegram: "用 Bot Token 接入 Telegram，接收私聊消息。",
  discord: "配置 Discord 机器人连接，集中管理运行状态。",
};
const ownerPlaceholder: Record<ChannelPlatform, string> = {
  wechat: "平台侧的用户身份",
  feishu: "飞书用户 open_id（ou_…）",
  wecom: "企业微信用户身份",
  qq: "QQ 机器人身份",
  telegram: "Telegram 用户或机器人身份",
  discord: "Discord 用户或机器人身份",
};
const sendersPlaceholder: Record<ChannelPlatform, string> = {
  wechat: "留空表示仅宿主所有者",
  feishu: "每行一个飞书 open_id；留空表示仅宿主所有者",
  wecom: "留空表示仅宿主所有者",
  qq: "留空表示仅宿主所有者",
  telegram: "留空表示仅宿主所有者",
  discord: "留空表示仅宿主所有者",
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

type ChannelDialogState =
  | { kind: "create"; platform: ChannelPlatform }
  | { kind: "edit"; view: ChannelView }
  | { kind: "delete"; view: ChannelView }
  | { kind: "wechat" };

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
    readonly createdConnectionId: string | null = null,
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
  return { platform };
}

function writeRoute(route: ChannelRoute, mode: "push" | "replace") {
  const params = new URLSearchParams();
  if (route.platform) params.set("platform", route.platform);
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

function receiptConnectionId(receipt: ChannelRequestReceipt) {
  const connectionId = receipt.result?.connection_id;
  return typeof connectionId === "string" ? connectionId : null;
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
      // The receipt is gone: never claimed, aged out, or lost with a restored
      // database. There is no durable outcome to reconcile, so use a new ID.
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
  const creating = scope.startsWith("create:");
  const createdConnectionId = creating ? receiptConnectionId(receipt) : null;
  delete pending[scope];
  writePending(pending);
  throw new StaleChannelIntentError(
    createdConnectionId
      ? "上一次创建已完成；正在打开已创建的连接。"
      : creating
        ? "上一次创建已完成；连接状态正在刷新，请核对后重新提交。"
        : "上一次提交已完成；连接状态正在刷新，请核对后重新提交本次修改。",
    true,
    receiptRevision(receipt),
    createdConnectionId,
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

const POLL_TIMEOUT_MS = 15_000;

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
      // A request that never settles must not hold the single-flight slot
      // forever; abort it and let the next visible tick try again.
      let timedOut = false;
      const timeout = window.setTimeout(() => {
        timedOut = true;
        current.abort();
      }, POLL_TIMEOUT_MS);
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
        if (stopped || epoch.current !== requestEpoch) return;
        if (timedOut) {
          callbacks.current.onError(new Error("连接状态读取超时，正在重试。"));
        } else if (!current.signal.aborted) {
          callbacks.current.onError(reason);
        }
      } finally {
        window.clearTimeout(timeout);
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
      ) : platform === "feishu" ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M6.2 4.8h11.6c1.6 0 2.9 1.3 2.9 2.9v7.1c0 1.6-1.3 2.9-2.9 2.9h-5.1l-4.2 2.7v-2.7H6.2c-1.6 0-2.9-1.3-2.9-2.9V7.7c0-1.6 1.3-2.9 2.9-2.9Z" />
          <path d="m8.2 9.3 7.1 2.4-3.2 1.2-1.1 3.2-2.8-6.8Z" />
        </svg>
      ) : platform === "wecom" ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M4.2 19.4V7.1c0-1.2 1-2.2 2.2-2.2h7.2c1 0 1.8.8 1.8 1.8v12.7" />
          <path d="M15.4 10.4h2.6c1.1 0 1.9.9 1.9 1.9v7.1" />
          <path d="M3 19.4h18" />
          <path d="M7.6 9h4.4M7.6 12.8h4.4" />
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
  busy,
  onLogin,
}: {
  status: WechatStatus | null;
  error: string;
  busy: boolean;
  onLogin: () => void;
}) {
  const state = status?.running
    ? "running"
    : status?.login_pending
      ? "connecting"
      : "stopped";
  return (
    <section className="wb-card channels-legacy" aria-label="微信宿主托管" data-channel-animate>
      <div className="channels-legacy-head">
        <PlatformMark platform="wechat" />
        <div className="channels-legacy-title">
          <h2>宿主托管的微信</h2>
          <span className="wb-muted">
            沿用 Server 现有的单一登录会话，不创建第二个微信 Controller。
          </span>
        </div>
        {status && <StatusPill state={state} kind="actual" />}
      </div>
      {status ? (
        <p className="wb-muted channels-legacy-meta">
          {status.enabled ? "宿主已启用" : "宿主已停用"}
          {" · "}
          {status.running
            ? "后台接收中"
            : status.login_pending
              ? "等待登录"
              : "当前未运行"}
          {" · "}
          {status.account_saved ? "已保存登录账号" : "尚未保存登录账号"}
        </p>
      ) : (
        <p className="wb-muted" role="status">
          {error || "正在读取微信登录状态…"}
        </p>
      )}
      {status?.verification_required && (
        <p className="channels-note" role="alert">
          微信要求输入登录验证码，请打开登录面板完成验证。
        </p>
      )}
      <div className="channels-legacy-actions">
        <button className="primary" disabled={!status || busy} onClick={onLogin}>
          登录微信
        </button>
        {status?.qr_image && (
          <span className="wb-muted">二维码已生成，打开登录面板扫码。</span>
        )}
      </div>
    </section>
  );
}

function ChannelDialog({
  title,
  subtitle,
  narrow = false,
  onClose,
  children,
}: {
  title: string;
  subtitle?: string;
  narrow?: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog || dialog.open) return;
    dialog.showModal();
    // Native dialogs focus the first control, which is the close button.
    // Prefer the control the dialog marks as its starting point instead.
    dialog.querySelector<HTMLElement>("[data-autofocus]")?.focus();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`channels-dialog${narrow ? " channels-dialog-narrow" : ""}`}
      aria-label={title}
      onCancel={event => {
        event.preventDefault();
        onClose();
      }}
    >
      <div className="channels-dialog-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p className="wb-muted">{subtitle}</p>}
        </div>
        <button
          type="button"
          className="channels-dialog-close"
          aria-label="关闭"
          title="关闭"
          onClick={onClose}
        >
          <Icon name="close" size={17} />
        </button>
      </div>
      <div className="channels-dialog-body">{children}</div>
    </dialog>
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
  onCreatedConnection,
}: {
  platform: ChannelPlatform;
  capability: ChannelCapabilities;
  initial: ChannelConnection | null;
  onCancel: () => void;
  onSaved: (view: ChannelView, created: boolean) => void;
  onDirty: () => void;
  onReconciled: () => void;
  onCreatedConnection: (connectionId: string) => void;
}) {
  const editing = initial !== null;
  // New connections are retained and runnable by default; editing does not
  // silently re-enable a connection the user deliberately stopped.
  const enabled = initial?.enabled ?? true;
  const [displayName, setDisplayName] = useState(initial?.display_name ?? "");
  const [owner, setOwner] = useState(initial?.owner_identity ?? "");
  const [senders, setSenders] = useState(
    (initial?.allowed_senders ?? []).join("\n"),
  );
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [error, setError] = useState<{
    text: string;
    kind: "validation" | "transport" | "stale-intent";
  } | null>(null);
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
      setError({ text: "请填写连接名称。", kind: "validation" });
      return;
    }
    if (replacing) {
      const missing = fields.filter(
        field => field.required && !filled[field.key],
      );
      if (missing.length) {
        setError({
          text: `替换凭据需要填写：${missing.map(field => field.label).join("、")}`,
          kind: "validation",
        });
        return;
      }
    } else if (!editing) {
      const missing = fields.filter(field => field.required);
      if (missing.length) {
        setError({
          text: `请填写凭据：${missing.map(field => field.label).join("、")}`,
          kind: "validation",
        });
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
    setError(null);
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
        if (reason.createdConnectionId) {
          onCreatedConnection(reason.createdConnectionId);
          return;
        }
        setError({ text: reason.message, kind: "stale-intent" });
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
      setError({ text: messageOf(reason), kind: "transport" });
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
          autoFocus
          data-autofocus
          maxLength={120}
          value={displayName}
          onChange={event => {
            setDisplayName(event.target.value);
            onDirty();
          }}
          placeholder={`例如：我的 ${platformName[platform]}`}
        />
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
          placeholder={ownerPlaceholder[platform]}
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
          placeholder={sendersPlaceholder[platform]}
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
          data-channel-error={error.kind}
        >
          {error.text}
        </p>
      )}
      <div className="channels-form-actions">
        <button className="primary" disabled={busy}>
          {busy ? "保存中…" : editing ? "保存修改" : "连接"}
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
  const [busy, setBusy] = useState<
    "" | "start" | "stop" | "reconnect" | "delete"
  >("");
  const [dialog, setDialog] = useState<ChannelDialogState | null>(null);
  const [dialogError, setDialogError] = useState("");
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [wechat, setWechat] = useState<WechatStatus | null>(null);
  const [wechatError, setWechatError] = useState("");
  const [wechatBusy, setWechatBusy] = useState(false);
  const [wechatCode, setWechatCode] = useState("");
  const [wechatNotice, setWechatNotice] = useState("");
  const [wechatActionError, setWechatActionError] = useState("");
  const pageRef = useRef<HTMLDivElement>(null);
  const pollEpoch = useRef(0);

  const navigate = useCallback((next: ChannelRoute, mode: "push" | "replace") => {
    writeRoute(next, mode);
    setRoute(next);
    setActionError("");
    setActionNotice("");
    setMenuFor(null);
    document.querySelector(".wb-main")?.scrollTo(0, 0);
  }, []);

  useEffect(() => {
    const update = () => {
      setRoute(readRoute());
      setActionError("");
      setActionNotice("");
      setDialog(null);
      setMenuFor(null);
    };
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  // Old links may still carry connection_id/mode; only the platform is part
  // of the second-level view, so rewrite them to the canonical URL.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    if ([...params.keys()].some(key => key !== "platform")) {
      writeRoute(readRoute(), "replace");
    }
  }, []);

  useEffect(() => {
    if (!menuFor) return;
    const close = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (!target?.closest(".channels-menu-wrap")) setMenuFor(null);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuFor(null);
    };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", escape);
    return () => {
      window.removeEventListener("pointerdown", close);
      window.removeEventListener("keydown", escape);
    };
  }, [menuFor]);

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
  const loading = !overview && !overviewError;
  const animationKey = platform
    ? `platform:${platform}:${platformViews
        .map(view => `${view.connection.connection_id}:${view.runtime.actual_state}`)
        .join(",")}`
    : `overview:${(overview?.platforms ?? [])
        .map(row => `${row.platform}:${row.display_state}:${row.connections}:${row.running}`)
        .join(",")}`;

  useChannelAnimation(pageRef, animationKey);

  function refreshChannels() {
    pollEpoch.current += 1;
    setRevision(value => value + 1);
  }

  function handleStaleIntent(reason: StaleChannelIntentError) {
    setActionError(reason.message);
    if (reason.reconciled) refreshChannels();
  }

  async function runAction(
    action: "start" | "stop" | "reconnect",
    view: ChannelView,
  ) {
    if (busy) return;
    const connectionId = view.connection.connection_id;
    const key = `action:${action}:${connectionId}`;
    const identity = `action:${action}:${connectionId}`;
    setMenuFor(null);
    setBusy(action);
    setActionError("");
    setActionNotice("");
    let requestId = "";
    try {
      requestId = (await requestIdFor(key, identity, null)).requestId;
      const updated = await channelAction(connectionId, action, requestId);
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
                row.connection.connection_id ===
                updated.connection.connection_id
                  ? updated
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

  async function removeConnection(view: ChannelView) {
    if (busy) return;
    const connectionId = view.connection.connection_id;
    const revision = view.connection.revision;
    const key = `delete:${connectionId}:${revision}`;
    const identity = `delete:${connectionId}:${revision}`;
    setBusy("delete");
    setDialogError("");
    let requestId = "";
    try {
      requestId = (await requestIdFor(key, identity, null)).requestId;
      await deleteChannelConnection(connectionId, {
        expected_revision: revision,
        request_id: requestId,
        confirm: true,
      });
      clearRequestId(key, requestId);
      setDialog(null);
      setActionNotice(`已删除「${view.connection.display_name}」。`);
      refreshChannels();
    } catch (reason) {
      if (reason instanceof StaleChannelIntentError) {
        setDialogError(reason.message);
        if (reason.reconciled) refreshChannels();
        return;
      }
      if (!shouldKeepRequestId(reason) && requestId) {
        clearRequestId(key, requestId);
      }
      setDialogError(messageOf(reason));
    } finally {
      setBusy("");
    }
  }

  function closeDialog() {
    setDialog(null);
    setDialogError("");
  }

  function openCreate() {
    if (!platform) return;
    setDialogError("");
    setDialog({ kind: "create", platform });
  }

  function openWechatLogin() {
    setMenuFor(null);
    setDialogError("");
    setWechatCode("");
    setWechatNotice("");
    setWechatActionError("");
    setDialog({ kind: "wechat" });
  }

  async function runWechat(args: string) {
    if (wechatBusy) return;
    setWechatBusy(true);
    setWechatActionError("");
    setWechatNotice("");
    try {
      await runtimeApi("commands", {
        command: "/wx",
        args,
        source: "web",
        request_id: newRequestId(),
        confirmed: false,
      });
      setWechatNotice(
        args === "stop"
          ? "已提交停止微信接收。"
          : args === "login"
            ? "已提交重新登录，二维码稍后刷新。"
            : args.startsWith("verify ")
              ? "已提交验证码，状态稍后刷新。"
              : "已提交微信登录，二维码稍后出现。",
      );
      refreshChannels();
    } catch (reason) {
      setWechatActionError(messageOf(reason));
    } finally {
      setWechatBusy(false);
    }
  }

  async function submitWechatCode(event: FormEvent) {
    event.preventDefault();
    const code = wechatCode.trim();
    if (!code) {
      setWechatActionError("请输入验证码。");
      return;
    }
    await runWechat(`verify ${code}`);
    setWechatCode("");
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
            description="管理微信、飞书、企业微信、QQ、Telegram 与 Discord 的连接和运行状态"
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
                      navigate({ platform: candidate }, "push")
                    }
                  >
                    <span className="channels-platform-top">
                      <PlatformMark platform={candidate} />
                      <span className="channels-platform-copy">
                        <strong>{platformName[candidate]}</strong>
                        <span className="wb-muted">
                          已配置 {row?.connections ?? 0} · 运行中 {row?.running ?? 0}
                        </span>
                      </span>
                    </span>
                    <span className="channels-platform-foot">
                      <span className="channels-platform-note">
                        {platformBlurb[candidate]}
                      </span>
                      <StatusPill state={state} />
                    </span>
                  </button>
                );
              })}
            </div>
          )}
          <p className="wb-muted channels-footnote">
            六个平台固定展示；状态、配置数和运行数均取自 Server 的真实连接状态。
          </p>
        </>
      )}

      {platform && (
        <>
          <button
            className="wb-back"
            onClick={() => navigate({ platform: "" }, "push")}
          >
            ‹ 返回总览
          </button>
          <PageHeader
            icon="server"
            title={
              platform === "wechat"
                ? "微信连接"
                : `${platformName[platform]} 连接`
            }
            description={
              platform === "wechat"
                ? "扫码登录，复用宿主现有的微信会话"
                : summary
                  ? `已配置 ${summary.connections} · 运行中 ${summary.running}`
                  : undefined
            }
            actions={
              capability?.supported_actions.includes("create") ? (
                <button className="primary" onClick={openCreate}>
                  <Icon name="plus" size={15} />
                  新建连接
                </button>
              ) : undefined
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
          {capability && !capability.adapter_supported && (
            <p className="channels-note" role="note">
              {capability.legacy_managed
                ? "宿主尚未启用微信会话，登录与生命周期操作暂不可用。"
                : "Server 尚未实现该平台的 Adapter。连接可以保存和编辑，但不会显示为运行中，启动操作也不可用。"}
            </p>
          )}
          {platform === "wechat" && (
            <WechatLegacyPanel
              status={wechat}
              error={wechatError}
              busy={wechatBusy}
              onLogin={openWechatLogin}
            />
          )}
          {loading ? (
            <p className="wb-muted" role="status">
              正在读取连接列表…
            </p>
          ) : platformViews.length ? (
            <div className="channels-list">
              {platformViews.map(view => {
                const connection = view.connection;
                const supported = capability?.supported_actions ?? [];
                const menuOpen = menuFor === connection.connection_id;
                return (
                  <article
                    className="wb-card channels-connection"
                    data-channel-animate
                    key={connection.connection_id}
                  >
                    <div className="channels-connection-top">
                      <div className="channels-connection-main">
                        <strong>
                          {connection.display_name}
                          {connection.legacy && (
                            <em className="channels-legacy-badge">宿主托管</em>
                          )}
                        </strong>
                        <span className="wb-muted">
                          {connection.connection_id} · v{connection.revision}
                        </span>
                        {view.runtime.last_error && (
                          <span className="channels-error-line" role="note">
                            最近错误：{view.runtime.last_error}
                          </span>
                        )}
                      </div>
                      <StatusPill state={view.runtime.actual_state} kind="actual" />
                      <div className="channels-menu-wrap">
                        <button
                          type="button"
                          className="channels-more"
                          aria-label={`${connection.display_name} 的操作`}
                          title="更多操作"
                          aria-haspopup="menu"
                          aria-expanded={menuOpen}
                          disabled={Boolean(busy)}
                          onClick={() =>
                            setMenuFor(
                              menuOpen ? null : connection.connection_id,
                            )
                          }
                        >
                          <svg
                            width="16"
                            height="16"
                            viewBox="0 0 24 24"
                            fill="currentColor"
                            aria-hidden="true"
                          >
                            <circle cx="5" cy="12" r="1.8" />
                            <circle cx="12" cy="12" r="1.8" />
                            <circle cx="19" cy="12" r="1.8" />
                          </svg>
                        </button>
                        {menuOpen && (
                          <div className="channels-menu" role="menu">
                            {supported.includes("start") &&
                              view.runtime.actual_state !== "running" && (
                                <button
                                  type="button"
                                  role="menuitem"
                                  disabled={Boolean(busy)}
                                  onClick={() => void runAction("start", view)}
                                >
                                  <Icon name="play" size={14} />
                                  {busy === "start" ? "启动中…" : "启动"}
                                </button>
                              )}
                            {supported.includes("stop") &&
                              view.runtime.actual_state !== "stopped" && (
                                <button
                                  type="button"
                                  role="menuitem"
                                  disabled={Boolean(busy)}
                                  onClick={() => void runAction("stop", view)}
                                >
                                  <Icon name="stop" size={14} />
                                  {busy === "stop" ? "停止中…" : "停止"}
                                </button>
                              )}
                            {supported.includes("reconnect") && (
                              <button
                                type="button"
                                role="menuitem"
                                disabled={Boolean(busy)}
                                onClick={() => void runAction("reconnect", view)}
                              >
                                <Icon name="refresh" size={14} />
                                {busy === "reconnect" ? "重连中…" : "重连"}
                              </button>
                            )}
                            {connection.legacy && (
                              <button
                                type="button"
                                role="menuitem"
                                onClick={openWechatLogin}
                              >
                                <Icon name="settings" size={14} />
                                登录微信
                              </button>
                            )}
                            {!connection.legacy &&
                              supported.includes("update") && (
                                <button
                                  type="button"
                                  role="menuitem"
                                  disabled={Boolean(busy)}
                                  onClick={() => {
                                    setMenuFor(null);
                                    setDialogError("");
                                    setDialog({ kind: "edit", view });
                                  }}
                                >
                                  <Icon name="edit" size={14} />
                                  编辑
                                </button>
                              )}
                            {!connection.legacy &&
                              supported.includes("delete") && (
                                <button
                                  type="button"
                                  role="menuitem"
                                  className="danger"
                                  disabled={Boolean(busy)}
                                  onClick={() => {
                                    setMenuFor(null);
                                    setDialogError("");
                                    setDialog({ kind: "delete", view });
                                  }}
                                >
                                  <Icon name="trash" size={14} />
                                  删除
                                </button>
                              )}
                          </div>
                        )}
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          ) : platform === "wechat" ? null : (
            <div className="wb-card channels-empty">
              <p>{`还没有 ${platformName[platform]} 连接。使用右上角的「新建连接」开始。`}</p>
            </div>
          )}
        </>
      )}

      {dialog?.kind === "create" && (
        <ChannelDialog
          title={`新建 ${platformName[dialog.platform]} 连接`}
          subtitle="凭据只写入，不会回显。"
          onClose={closeDialog}
        >
          {capabilities[dialog.platform] ? (
            <ConnectionForm
              key={`create:${dialog.platform}`}
              platform={dialog.platform}
              capability={capabilities[dialog.platform]}
              initial={null}
              onCancel={closeDialog}
              onDirty={() => {
                setActionError("");
                setActionNotice("");
              }}
              onReconciled={refreshChannels}
              onCreatedConnection={() => {
                refreshChannels();
                closeDialog();
                setActionNotice("连接已创建。");
              }}
              onSaved={() => {
                refreshChannels();
                closeDialog();
                setActionNotice("连接已创建。");
              }}
            />
          ) : (
            <p className="wb-muted" role="status">
              {capabilityError || "正在读取平台能力…"}
            </p>
          )}
        </ChannelDialog>
      )}

      {dialog?.kind === "edit" && (
        <ChannelDialog
          title="编辑连接"
          subtitle={dialog.view.connection.display_name}
          onClose={closeDialog}
        >
          {capabilities[dialog.view.connection.platform] ? (
            <ConnectionForm
              key={`edit:${dialog.view.connection.connection_id}`}
              platform={dialog.view.connection.platform}
              capability={capabilities[dialog.view.connection.platform]}
              initial={dialog.view.connection}
              onCancel={closeDialog}
              onDirty={() => {
                setActionError("");
                setActionNotice("");
              }}
              onReconciled={refreshChannels}
              onCreatedConnection={() => undefined}
              onSaved={() => {
                refreshChannels();
                closeDialog();
                setActionNotice("连接修改已保存。");
              }}
            />
          ) : (
            <p className="wb-muted" role="status">
              {capabilityError || "正在读取平台能力…"}
            </p>
          )}
        </ChannelDialog>
      )}

      {dialog?.kind === "delete" && (
        <ChannelDialog
          title="删除连接"
          subtitle={dialog.view.connection.display_name}
          narrow
          onClose={closeDialog}
        >
          <p className="channels-dialog-copy">
            删除后无法恢复，审计记录会保留。确定删除「
            {dialog.view.connection.display_name}」？
          </p>
          {dialogError && (
            <p className="channels-error" role="alert">
              {dialogError}
            </p>
          )}
          <div className="channels-dialog-actions">
            <button
              className="channels-danger primary"
              disabled={Boolean(busy)}
              onClick={() => void removeConnection(dialog.view)}
            >
              {busy === "delete" ? "删除中…" : "确认删除"}
            </button>
            <button data-autofocus disabled={Boolean(busy)} onClick={closeDialog}>
              取消
            </button>
          </div>
        </ChannelDialog>
      )}

      {dialog?.kind === "wechat" && (
        <ChannelDialog
          title="登录微信"
          subtitle="复用宿主现有的登录会话；关闭面板不会中断登录。"
          narrow
          onClose={closeDialog}
        >
          <div className="channels-wechat">
            {wechat ? (
              <>
                <p
                  className="channels-legacy-status"
                  role="status"
                  data-wechat-status={wechat.status}
                >
                  {wechat.status}
                </p>
                <p className="wb-muted">
                  {wechat.enabled ? "宿主已启用" : "宿主已停用"}
                  {" · "}
                  {wechat.running
                    ? "后台接收中"
                    : wechat.login_pending
                      ? "等待扫码"
                      : "当前未运行"}
                  {" · "}
                  {wechat.account_saved ? "已保存登录账号" : "尚未保存登录账号"}
                </p>
                {wechat.qr_image && (
                  <figure className="channels-qr">
                    <img
                      src={wechat.qr_image}
                      alt="微信登录二维码"
                      width="200"
                      height="200"
                    />
                    <figcaption>使用微信扫码登录</figcaption>
                  </figure>
                )}
                {wechat.login_pending && (
                  <p
                    className="channels-note"
                    role="status"
                    data-wechat-notice={
                      wechat.verification_required ? "verification" : "scan"
                    }
                  >
                    {wechat.verification_required
                      ? "扫码后还需要输入登录验证码。"
                      : "等待扫码。登录流程由 Server 继续，关闭面板不会中断。"}
                  </p>
                )}
                {wechat.verification_required && (
                  <form className="channels-verify" onSubmit={submitWechatCode}>
                    <label>
                      登录验证码
                      <input
                        autoComplete="one-time-code"
                        maxLength={50}
                        value={wechatCode}
                        disabled={wechatBusy}
                        onChange={event => setWechatCode(event.target.value)}
                        placeholder="输入验证码"
                      />
                    </label>
                    <button
                      className="primary"
                      disabled={wechatBusy || !wechatCode.trim()}
                    >
                      提交验证码
                    </button>
                  </form>
                )}
                <div className="channels-dialog-actions">
                  <button
                    className="primary"
                    data-autofocus
                    disabled={wechatBusy || wechat.running || wechat.login_pending}
                    onClick={() => void runWechat("")}
                  >
                    {wechat.account_saved ? "启动已登录账号" : "登录微信"}
                  </button>
                  <button
                    disabled={wechatBusy || wechat.running || wechat.login_pending}
                    onClick={() => void runWechat("login")}
                  >
                    重新扫码登录
                  </button>
                  <button
                    disabled={wechatBusy || (!wechat.running && !wechat.login_pending)}
                    onClick={() => void runWechat("stop")}
                  >
                    停止接收
                  </button>
                </div>
              </>
            ) : (
              <p className="wb-muted" role="status">
                {wechatError || "正在读取微信登录状态…"}
              </p>
            )}
            {wechatNotice && (
              <p className="wb-muted" role="status">
                {wechatNotice}
              </p>
            )}
            {wechatActionError && (
              <p className="channels-error" role="alert">
                {wechatActionError}
              </p>
            )}
          </div>
        </ChannelDialog>
      )}
    </div>
  );
}
