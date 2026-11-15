import { useCallback, useEffect, useRef, useState } from "react";
import { newRequestId } from "../ids";
import {
  createDailyReview,
  createWeeklyReview,
  getReview,
  listReviews,
  resendDelivery,
  type Delivery,
  type DeliveryStatus,
  type ReviewDetail,
  type ReviewStatus,
  type ReviewSummary,
  type ReviewType,
} from "../review-api";
import { RuntimeError, runtimeApi } from "../runtime-api";
import { dateLabel } from "./data";
import { Drawer, Markdown } from "./evidence";
import { Empty, ErrorText } from "./execution";
import { Icon } from "./Icon";

const PAGE_SIZE = 20;
const POLL_MS = 2500;
const WEEK_PATTERN = /^\d{4}-W\d{2}$/;
const DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const PENDING_KEY = "cairn.web.pending.review-generation";
const DELIVERY_KEY = "cairn.web.pending.review-delivery";

const statusLabel: Record<ReviewStatus, string> = {
  pending: "待生成",
  generating: "生成中",
  completed: "已完成",
  failed: "生成失败",
};

const deliveryLabel: Record<DeliveryStatus, string> = {
  pending: "等待投递",
  blocked_offline: "微信离线，等待重试",
  sending: "投递中",
  accepted: "已接收",
  delivery_unknown: "投递结果不明",
  failed_permanent: "投递失败",
};

const generationErrorText: Record<string, string> = {
  provider_unavailable: "Server 未配置模型 Provider，本次没有调用模型。",
  model_timeout: "模型调用超时。",
  model_error: "模型调用失败。",
  empty_model_response: "模型返回为空。",
  invalid_model_json: "模型输出不是有效 JSON。",
  invalid_model_output: "模型输出未通过格式校验。",
  fabricated_completion: "模型把没有完成证据的工作写成了进展，报告被拒绝。",
  missing_source_reference: "模型遗漏了来源引用，报告被拒绝。",
  invalid_source_reference: "模型引用了不存在的来源，报告被拒绝。",
  model_tool_calls_forbidden: "模型尝试调用工具，报告被拒绝。",
  model_output_too_large: "模型输出超出限制。",
  generation_cancelled: "生成在完成前被取消。",
};

function generationError(code: string | null | undefined) {
  if (!code) return "生成失败，原因未记录。";
  return generationErrorText[code] ?? `生成失败: ${code}`;
}

type ReviewRoute = {
  type: ReviewType;
  day: string;
  week: string;
  review: string;
};

function readRoute(): ReviewRoute {
  const params = new URLSearchParams(location.search);
  const type: ReviewType = params.get("type") === "weekly" ? "weekly" : "daily";
  const day = type === "daily" ? params.get("day") ?? "" : "";
  const week = type === "weekly" ? (params.get("week") ?? "").toUpperCase() : "";
  return {
    type,
    day: DAY_PATTERN.test(day) ? day : "",
    week: WEEK_PATTERN.test(week) ? week : "",
    review: params.get("review") ?? "",
  };
}

function writeRoute(route: ReviewRoute, mode: "push" | "replace") {
  const params = new URLSearchParams(location.search);
  params.set("pane", "reviews");
  params.set("type", route.type);
  if (route.day) params.set("day", route.day);
  else params.delete("day");
  if (route.week) params.set("week", route.week);
  else params.delete("week");
  if (route.review) params.set("review", route.review);
  else params.delete("review");
  const query = params.toString();
  const url = location.pathname + (query ? `?${query}` : "");
  history[mode === "push" ? "pushState" : "replaceState"]({}, "", url);
}

function mergeReviews(
  current: ReviewSummary[],
  incoming: ReviewSummary[],
): ReviewSummary[] {
  const byId = new Map(current.map(item => [item.review_id, item]));
  for (const item of incoming) byId.set(item.review_id, item);
  return [...byId.values()].sort(
    (left, right) =>
      right.created_at.localeCompare(left.created_at) ||
      right.review_id.localeCompare(left.review_id),
  );
}

function periodLabel(item: ReviewSummary) {
  if (item.review_type === "weekly") return item.week_id ?? item.date;
  return item.date;
}

function todayLocal() {
  const now = new Date();
  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
}

function isoWeekId(value: Date) {
  const date = new Date(
    Date.UTC(value.getFullYear(), value.getMonth(), value.getDate()),
  );
  const weekday = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - weekday);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const week = Math.ceil(
    ((date.getTime() - yearStart.getTime()) / 86_400_000 + 1) / 7,
  );
  return `${date.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

type PendingRequest = { key: string; request_id: string; at: string };

function readPendingRequest(storageKey: string): PendingRequest | null {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object") return null;
    const item = value as Record<string, unknown>;
    if (
      typeof item.key !== "string" ||
      typeof item.request_id !== "string" ||
      typeof item.at !== "string"
    ) {
      localStorage.removeItem(storageKey);
      return null;
    }
    return item as PendingRequest;
  } catch {
    return null;
  }
}

function requestIdFor(storageKey: string, key: string) {
  const current = readPendingRequest(storageKey);
  if (current?.key === key) return current.request_id;
  const requestId = newRequestId();
  try {
    localStorage.setItem(
      storageKey,
      JSON.stringify({
        key,
        request_id: requestId,
        at: new Date().toISOString(),
      } satisfies PendingRequest),
    );
  } catch {
    /* best effort: the request id still lasts for this component */
  }
  return requestId;
}

function clearRequestId(storageKey: string, requestId: string) {
  try {
    const current = readPendingRequest(storageKey);
    if (current?.request_id === requestId) localStorage.removeItem(storageKey);
  } catch {
    /* best effort */
  }
}

function isActive(status: ReviewStatus) {
  return status === "pending" || status === "generating";
}

function shouldKeepRequestId(reason: unknown) {
  return (
    reason instanceof RuntimeError && (reason.status === 0 || reason.status >= 500)
  );
}

function useListAnimation(
  ref: React.RefObject<HTMLDivElement | null>,
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
        const cards = ref.current?.querySelectorAll(".review-card");
        if (cards?.length) {
          gsap.from(cards, {
            y: 10,
            autoAlpha: 0,
            duration: 0.3,
            stagger: 0.03,
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

function useDetailAnimation(
  ref: React.RefObject<HTMLDivElement | null>,
  reviewId: string | null | undefined,
) {
  useEffect(() => {
    if (!reviewId) return;
    let cancelled = false;
    let media: { revert: () => void } | undefined;
    void import("gsap").then(({ gsap }) => {
      if (cancelled || !ref.current) return;
      const context = gsap.matchMedia();
      media = context;
      context.add("(prefers-reduced-motion: no-preference)", () => {
        if (ref.current) {
          gsap.from(ref.current, {
            y: 12,
            autoAlpha: 0,
            duration: 0.24,
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
  }, [ref, reviewId]);
}

function DeliveryPanel({
  review,
  delivery,
  onChanged,
}: {
  review: ReviewDetail;
  delivery: Delivery;
  onChanged: () => void;
}) {
  const retryable =
    delivery.status === "failed_permanent" ||
    delivery.status === "delivery_unknown" ||
    delivery.status === "blocked_offline";
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [feedback, setFeedback] = useState("");

  async function resend() {
    if (busy) return;
    setBusy(true);
    setError("");
    setFeedback("");
    const key = `${review.review_id}:${delivery.updated_at}`;
    const requestId = requestIdFor(DELIVERY_KEY, key);
    try {
      await resendDelivery(review.review_id, { requestId, rebind: true });
      clearRequestId(DELIVERY_KEY, requestId);
      setFeedback("已重新入队；微信可用后由 Server 发送，不会立即送达。");
      onChanged();
    } catch (reason) {
      if (!shouldKeepRequestId(reason)) {
        clearRequestId(DELIVERY_KEY, requestId);
      }
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="review-delivery">
      <h3>投递状态</h3>
      <div className="review-delivery-row">
        <span className="review-delivery-status" data-status={delivery.status}>
          {deliveryLabel[delivery.status]}
        </span>
        <span className="wb-muted">尝试 {delivery.attempts} 次</span>
        {delivery.attempted_at && (
          <span className="wb-muted">最近 {dateLabel(delivery.attempted_at)}</span>
        )}
      </div>
      {delivery.last_error && (
        <p className="wb-muted">错误: {delivery.last_error}</p>
      )}
      {delivery.status === "delivery_unknown" && (
        <p className="review-warning" role="alert">
          上次发送结果不明，消息可能已经送达。重新投递可能产生重复消息，需要人工确认。
        </p>
      )}
      {retryable && (
        <div className="review-resend">
          {delivery.status === "delivery_unknown" && (
            <label>
              <input
                type="checkbox"
                checked={confirmed}
                onChange={event => setConfirmed(event.target.checked)}
              />
              我已确认可能重复发送
            </label>
          )}
          <button
            disabled={
              busy || (delivery.status === "delivery_unknown" && !confirmed)
            }
            onClick={() => void resend()}
          >
            {busy ? "提交中…" : "重新投递"}
          </button>
          <ErrorText value={error} />
        </div>
      )}
      {feedback && (
        <p className="wb-muted" role="status">
          {feedback}
        </p>
      )}
    </section>
  );
}

function SourceEvidence({ review }: { review: ReviewDetail }) {
  return (
    <>
      <section className="review-sources">
        <h3>来源清单</h3>
        {review.source_manifest.length ? (
          <div className="review-table-wrap">
            <table className="review-table">
              <thead>
                <tr>
                  <th>来源</th>
                  <th>类型</th>
                  <th>可信度</th>
                  <th>完成证据</th>
                  <th>纳入</th>
                  <th>验证</th>
                </tr>
              </thead>
              <tbody>
                {review.source_manifest.map(entry => (
                  <tr key={entry.source_id}>
                    <td title={entry.source_id}>
                      <code>{entry.source_id}</code>
                    </td>
                    <td>{entry.source_type}</td>
                    <td>{entry.trust}</td>
                    <td>{entry.may_prove_completion ? "是" : "否"}</td>
                    <td>
                      {entry.included
                        ? "是"
                        : `否 (${entry.omitted_reason ?? "未纳入"})`}
                    </td>
                    <td title={entry.verification}>
                      {entry.truncated ? "已截断" : "完整"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="wb-muted">这次报告没有来源清单。</p>
        )}
      </section>
      <section className="review-collection">
        <h3>采集状态</h3>
        {review.collection_status.map(status => (
          <div className="review-collection-row" key={status.source_type}>
            <strong>{status.source_type}</strong>
            <span>{status.status}</span>
            <span className="wb-muted">
              发现 {status.discovered} · 纳入 {status.included} · 省略{" "}
              {status.omitted}
            </span>
            {status.notes.map((note, index) => (
              <small className="wb-muted" key={`${status.source_type}-${index}`}>
                {note}
              </small>
            ))}
          </div>
        ))}
      </section>
      <section className="review-snapshots">
        <h3>已保存证据</h3>
        <p className="wb-muted">
          只显示生成时实际纳入并已脱敏的正文；被省略的来源保留身份但不进入提示词。
        </p>
        {review.source_snapshots.length ? (
          review.source_snapshots.map(source => (
            <details key={source.source_id} className="review-snapshot">
              <summary>
                <Icon name="doc" size={14} />
                {source.source_id}
                <span className="wb-muted">
                  {source.trust} ·{" "}
                  {source.may_prove_completion ? "可证明完成" : "不能证明完成"}
                </span>
              </summary>
              <pre>{source.snapshot || "（无正文）"}</pre>
            </details>
          ))
        ) : (
          <p className="wb-muted">这次报告没有可显示的证据正文。</p>
        )}
      </section>
    </>
  );
}

export function Reviews() {
  const [route, setRoute] = useState<ReviewRoute>(readRoute);
  const [items, setItems] = useState<ReviewSummary[]>([]);
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [revision, setRevision] = useState(0);
  const [timezone, setTimezone] = useState("Asia/Shanghai");
  const [dayDraft, setDayDraft] = useState(route.day);
  const [weekDraft, setWeekDraft] = useState(route.week);
  const [filterError, setFilterError] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [genDay, setGenDay] = useState(todayLocal());
  const [genWeek, setGenWeek] = useState(isoWeekId(new Date(Date.now() - 604_800_000)));
  const [regenerate, setRegenerate] = useState(false);
  const [formError, setFormError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [detailBusy, setDetailBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const detailRef = useRef<HTMLDivElement>(null);
  const routeKey = `${route.type}|${route.day}|${route.week}`;
  const previousRouteKey = useRef(routeKey);

  useEffect(() => {
    const update = () => setRoute(readRoute());
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  useEffect(() => {
    setDayDraft(route.day);
    setWeekDraft(route.week);
  }, [route.day, route.week, route.type]);

  useEffect(() => {
    const controller = new AbortController();
    runtimeApi<{ review?: { timezone?: string } }>(
      "management?section=review",
      undefined,
      controller.signal,
    )
      .then(value => {
        if (value.review?.timezone) setTimezone(value.review.timezone);
      })
      .catch(() => {
        /* the default timezone still lets the operator pick a period */
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const changed = previousRouteKey.current !== routeKey;
    previousRouteKey.current = routeKey;
    const controller = new AbortController();
    let cancelled = false;
    if (changed) {
      setItems([]);
      setHasMore(false);
      setListError("");
    }
    setLoading(changed);
    listReviews(
      {
        type: route.type,
        day: route.day || undefined,
        weekId: route.week || undefined,
        limit: PAGE_SIZE,
        offset: 0,
      },
      controller.signal,
    )
      .then(data => {
        if (cancelled) return;
        setItems(current =>
          changed ? data.reviews : mergeReviews(current, data.reviews),
        );
        // A status poll only refreshes the first page. Do not resurrect the
        // load-more control after the operator has already reached the end.
        if (changed || items.length === 0) {
          setHasMore(data.reviews.length === PAGE_SIZE);
        }
      })
      .catch(reason => {
        if (!cancelled && !controller.signal.aborted) {
          setListError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [route.type, route.day, route.week, routeKey, revision]);

  useEffect(() => {
    if (!route.review) {
      setDetail(null);
      setDetailError("");
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    getReview(route.review, controller.signal)
      .then(value => {
        if (!cancelled) setDetail(value);
      })
      .catch(reason => {
        if (!cancelled && !controller.signal.aborted) {
          setDetailError(reason instanceof Error ? reason.message : String(reason));
        }
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [route.review, revision]);

  const listActive = items.some(item => isActive(item.status));
  const detailActive = detail !== null && isActive(detail.status);
  useEffect(() => {
    if (!listActive && !detailActive) return;
    const timer = setInterval(() => setRevision(value => value + 1), POLL_MS);
    return () => clearInterval(timer);
  }, [listActive, detailActive]);

  useListAnimation(
    listRef,
    loading ? "" : `${routeKey}:${items.length ? "ready" : "empty"}`,
  );
  useDetailAnimation(detailRef, detail?.review_id);

  const navigate = useCallback((next: ReviewRoute, mode: "push" | "replace") => {
    writeRoute(next, mode);
    setRoute(next);
    setActionError("");
  }, []);

  function switchType(next: ReviewType) {
    navigate(
      { type: next, day: "", week: "", review: "" },
      "push",
    );
    setFilterError("");
  }

  function applyFilter(event: React.FormEvent) {
    event.preventDefault();
    if (route.type === "daily") {
      if (!DAY_PATTERN.test(dayDraft)) {
        setFilterError("请选择有效的日期。");
        return;
      }
      navigate({ ...route, day: dayDraft, week: "", review: "" }, "push");
    } else {
      const week = weekDraft.trim().toUpperCase();
      if (!WEEK_PATTERN.test(week)) {
        setFilterError("周次格式应为 YYYY-Www，例如 2026-W03。");
        return;
      }
      navigate({ ...route, day: "", week, review: "" }, "push");
    }
    setFilterError("");
  }

  function clearFilter() {
    navigate({ ...route, day: "", week: "", review: "" }, "push");
    setFilterError("");
  }

  function openReview(reviewId: string) {
    navigate({ ...route, review: reviewId }, "push");
  }

  function closeReview() {
    navigate({ ...route, review: "" }, "replace");
  }

  async function loadMore() {
    if (loading || loadingMore || !hasMore) return;
    setLoadingMore(true);
    setListError("");
    try {
      const data = await listReviews({
        type: route.type,
        day: route.day || undefined,
        weekId: route.week || undefined,
        limit: PAGE_SIZE,
        offset: items.length,
      });
      setItems(current => mergeReviews(current, data.reviews));
      setHasMore(data.reviews.length === PAGE_SIZE);
    } catch (reason) {
      setListError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoadingMore(false);
    }
  }

  function openForm() {
    const yesterday = new Date(Date.now() - 86_400_000);
    const lastWeek = new Date(Date.now() - 604_800_000);
    setGenDay(
      route.type === "daily"
        ? [
            yesterday.getFullYear(),
            String(yesterday.getMonth() + 1).padStart(2, "0"),
            String(yesterday.getDate()).padStart(2, "0"),
          ].join("-")
        : todayLocal(),
    );
    setGenWeek(isoWeekId(route.type === "weekly" ? lastWeek : new Date()));
    setRegenerate(false);
    setFormError("");
    setFormOpen(true);
  }

  async function submitGeneration(event: React.FormEvent) {
    event.preventDefault();
    if (submitting) return;
    const period =
      route.type === "daily" ? genDay : genWeek.trim().toUpperCase();
    if (route.type === "daily" && !DAY_PATTERN.test(period)) {
      setFormError("请选择有效的日期。");
      return;
    }
    if (route.type === "weekly" && !WEEK_PATTERN.test(period)) {
      setFormError("周次格式应为 YYYY-Www，例如 2026-W03。");
      return;
    }
    const key = `generate:${route.type}:${period}:${regenerate ? "new" : "reuse"}`;
    const requestId = requestIdFor(PENDING_KEY, key);
    setSubmitting(true);
    setFormError("");
    try {
      const created =
        route.type === "daily"
          ? await createDailyReview({
              date: period,
              timezone,
              requestId,
              regenerate,
            })
          : await createWeeklyReview({
              weekId: period,
              timezone,
              requestId,
              regenerate,
            });
      clearRequestId(PENDING_KEY, requestId);
      setFormOpen(false);
      setRevision(value => value + 1);
      navigate({ ...route, review: created.review_id }, "push");
    } catch (reason) {
      if (!shouldKeepRequestId(reason)) clearRequestId(PENDING_KEY, requestId);
      setFormError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSubmitting(false);
    }
  }

  async function regenerateDetail() {
    if (!detail || detailBusy) return;
    const period =
      detail.review_type === "weekly"
        ? detail.week_id ?? detail.date
        : detail.date;
    const key = `regenerate:${detail.review_type}:${period}`;
    const requestId = requestIdFor(PENDING_KEY, key);
    setDetailBusy(true);
    setActionError("");
    try {
      const created =
        detail.review_type === "daily"
          ? await createDailyReview({
              date: period,
              timezone: detail.timezone,
              requestId,
              regenerate: true,
            })
          : await createWeeklyReview({
              weekId: period,
              timezone: detail.timezone,
              requestId,
              regenerate: true,
            });
      clearRequestId(PENDING_KEY, requestId);
      setRevision(value => value + 1);
      navigate({ ...route, review: created.review_id }, "push");
    } catch (reason) {
      if (!shouldKeepRequestId(reason)) clearRequestId(PENDING_KEY, requestId);
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setDetailBusy(false);
    }
  }

  const filtered = Boolean(route.day || route.week);
  const typeName = route.type === "daily" ? "日报" : "周报";

  return (
    <section className="reviews">
      <div className="review-toolbar">
        <div className="review-tabs" role="tablist" aria-label="复盘类型">
          {(["daily", "weekly"] as const).map(type => (
            <button
              key={type}
              role="tab"
              aria-selected={route.type === type}
              onClick={() => switchType(type)}
            >
              {type === "daily" ? "日报" : "周报"}
            </button>
          ))}
        </div>
        <form className="review-filter" onSubmit={applyFilter}>
          {route.type === "daily" ? (
            <label>
              <Icon name="clock" size={14} />
              <input
                type="date"
                aria-label="按日期筛选"
                value={dayDraft}
                onChange={event => setDayDraft(event.target.value)}
              />
            </label>
          ) : (
            <label>
              <Icon name="clock" size={14} />
              <input
                aria-label="按 ISO 周次筛选"
                placeholder="2026-W03"
                maxLength={8}
                value={weekDraft}
                onChange={event => setWeekDraft(event.target.value)}
              />
            </label>
          )}
          <button type="submit">筛选</button>
          {filtered && (
            <button type="button" onClick={clearFilter}>
              清除
            </button>
          )}
        </form>
        <button className="primary" onClick={openForm}>
          <Icon name="plus" size={15} />
          生成{typeName}
        </button>
      </div>
      <ErrorText value={filterError} />
      <ErrorText value={listError} />
      {detailError && <ErrorText value={detailError} />}
      {formOpen && (
        <section className="wb-card review-generate">
          <h2>生成{typeName}</h2>
          <form onSubmit={submitGeneration}>
            {route.type === "daily" ? (
              <label>
                日期
                <input
                  type="date"
                  required
                  value={genDay}
                  onChange={event => setGenDay(event.target.value)}
                />
              </label>
            ) : (
              <label>
                ISO 周次
                <input
                  required
                  placeholder="2026-W03"
                  maxLength={8}
                  value={genWeek}
                  onChange={event => setGenWeek(event.target.value)}
                />
              </label>
            )}
            <label className="review-regenerate">
              <input
                type="checkbox"
                checked={regenerate}
                onChange={event => setRegenerate(event.target.checked)}
              />
              生成新版本（该周期已有报告时）
            </label>
            <div className="review-form-actions">
              <button className="primary" disabled={submitting}>
                {submitting ? "提交中…" : "开始生成"}
              </button>
              <button
                type="button"
                disabled={submitting}
                onClick={() => setFormOpen(false)}
              >
                取消
              </button>
            </div>
            <ErrorText value={formError} />
            <p className="wb-muted">
              生成在 Server 后台进行，页面会轮询状态。手动生成不会自动发送微信；
              失败时用同一请求编号重试不会重复生成。
            </p>
          </form>
        </section>
      )}
      {!formOpen && listActive && (
        <p className="wb-muted" role="status">
          Server 正在生成报告，页面会自动刷新。
        </p>
      )}
      {loading && !items.length ? (
        <p className="wb-muted" role="status">
          正在读取复盘记录…
        </p>
      ) : items.length ? (
        <>
          <div className="review-list" ref={listRef}>
            {items.map(item => (
              <article className="review-card" key={item.review_id}>
                <button
                  className="review-card-main"
                  onClick={() => openReview(item.review_id)}
                >
                  <span className="review-meta">
                    <span
                      className="review-state"
                      data-status={item.status}
                    >
                      {statusLabel[item.status]}
                    </span>
                    <span>v{item.version}</span>
                    <time>{periodLabel(item)}</time>
                  </span>
                  <h2>
                    {item.review_type === "weekly"
                      ? `${periodLabel(item)} 周报`
                      : `${periodLabel(item)} 日报`}
                  </h2>
                  <p className="review-preview">
                    {item.overview ||
                      (item.status === "failed"
                        ? generationError(item.error)
                        : "报告尚未生成内容。")}
                  </p>
                </button>
                <footer>
                  <small>
                    {item.completed_at
                      ? `完成于 ${dateLabel(item.completed_at)}`
                      : `更新于 ${dateLabel(item.updated_at)}`}
                  </small>
                  <small>{item.timezone}</small>
                </footer>
              </article>
            ))}
          </div>
          {hasMore && (
            <div className="review-more">
              <button disabled={loadingMore} onClick={() => void loadMore()}>
                {loadingMore ? "加载中…" : "加载更多"}
              </button>
            </div>
          )}
        </>
      ) : !listError ? (
        <Empty
          icon="history"
          text={filtered ? "该筛选下没有复盘报告。" : "还没有生成任何复盘报告。"}
        />
      ) : null}
      {detail && (
        <Drawer
          title={`${detail.review_type === "weekly" ? "周报" : "日报"}详情`}
          close={closeReview}
        >
          <div className="review-detail" ref={detailRef}>
            <div className="review-detail-meta">
              <span className="review-state" data-status={detail.status}>
                {statusLabel[detail.status]}
              </span>
              <span>v{detail.version}</span>
              <span>{periodLabel(detail)}</span>
              <span>{detail.timezone}</span>
              {detail.supersedes_review_id && (
                <span>取代 {detail.supersedes_review_id.slice(0, 18)}…</span>
              )}
            </div>
            {detail.error && (
              <p role="alert" className="wb-error">
                {generationError(detail.error)}
              </p>
            )}
            <ErrorText value={actionError} />
            <div className="review-detail-actions">
              <button
                disabled={detailBusy || isActive(detail.status)}
                onClick={() => void regenerateDetail()}
              >
                {detailBusy ? "提交中…" : "重新生成"}
              </button>
              <small className="wb-muted">
                重新生成会创建新版本，旧版本保留；不会自动发送微信。
              </small>
            </div>
            {detail.summary_markdown ? (
              <Markdown text={detail.summary_markdown} />
            ) : (
              <p className="wb-muted">
                {isActive(detail.status)
                  ? "报告仍在生成，完成后会显示全文。"
                  : "这份报告没有可显示的正文。"}
              </p>
            )}
            {detail.delivery ? (
              <DeliveryPanel
                key={detail.review_id}
                review={detail}
                delivery={detail.delivery}
                onChanged={() => setRevision(value => value + 1)}
              />
            ) : (
              <p className="wb-muted">
                这条报告没有投递记录（手动生成的报告不会自动投递）。
              </p>
            )}
            <SourceEvidence review={detail} />
          </div>
        </Drawer>
      )}
    </section>
  );
}
