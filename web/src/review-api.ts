import { RuntimeError } from "./runtime-api";

export type ReviewType = "daily" | "weekly";
export type ReviewStatus = "pending" | "generating" | "completed" | "failed";
export type DeliveryStatus =
  | "pending"
  | "blocked_offline"
  | "sending"
  | "accepted"
  | "delivery_unknown"
  | "failed_permanent";

export type ReviewItem = { text: string; source_refs: string[] };

export type DailySummary = {
  date: string;
  overview: string;
  completed_activities: ReviewItem[];
  knowledge_insights: ReviewItem[];
  problems: ReviewItem[];
  recommendations: ReviewItem[];
  source_refs: string[];
  confidence_notes: string[];
};

export type WeeklySummary = {
  week_id: string;
  period_start: string;
  period_end: string;
  overview: string;
  major_events: ReviewItem[];
  progress: ReviewItem[];
  knowledge_insights: ReviewItem[];
  recurring_problems: ReviewItem[];
  next_week_priorities: ReviewItem[];
  source_refs: string[];
  confidence_notes: string[];
};

export type ReviewSummaryPayload = DailySummary | WeeklySummary;

export type ManifestEntry = {
  source_type: string;
  source_id: string;
  content_hash: string;
  occurred_at: string;
  trust: string;
  may_prove_completion: boolean;
  verification: string;
  included: boolean;
  omitted_reason: string | null;
  truncated: boolean;
};

export type CollectionStatus = {
  source_type: string;
  status: string;
  discovered: number;
  included: number;
  omitted: number;
  notes: string[];
};

export type SourceSnapshot = {
  source_type: string;
  source_id: string;
  content_hash: string;
  occurred_at: string;
  trust: string;
  may_prove_completion: boolean;
  verification: string;
  snapshot: string;
  truncated: boolean;
  included: boolean;
  omitted_reason: string | null;
};

export type Delivery = {
  delivery_id: string;
  review_id: string;
  review_version: number;
  channel: string;
  status: DeliveryStatus;
  attempt_id: string;
  attempts: number;
  last_error: string | null;
  created_at: string;
  attempted_at: string | null;
  completed_at: string | null;
  updated_at: string;
};

export type ReviewSummary = {
  review_id: string;
  review_type: ReviewType;
  date: string;
  week_id?: string;
  period_start: string;
  period_end: string;
  timezone: string;
  status: ReviewStatus;
  version: number;
  supersedes_review_id: string | null;
  input_fingerprint: string;
  model_id: string | null;
  generation_attempts: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  error: string | null;
  overview?: string;
};

export type ReviewDetail = ReviewSummary & {
  delivery: Delivery | null;
  source_manifest: ManifestEntry[];
  collection_status: CollectionStatus[];
  source_snapshots: SourceSnapshot[];
  summary: ReviewSummaryPayload | null;
  summary_markdown: string | null;
};

export type ReviewList = { reviews: ReviewSummary[] };

const FAILURE_TEXT: Record<string, string> = {
  invalid_review_request: "请求参数不符合要求，请检查日期或周次。",
  invalid_json: "请求内容不是有效的 JSON。",
  request_id_conflict: "该请求编号此前对应了不同操作，请重新发起。",
  review_period_completed: "该周期已有完成报告；生成新版本需要明确重新生成。",
  review_period_not_ended: "该复盘周期尚未结束，暂时不能生成。",
  review_not_found: "复盘报告不存在或已被删除。",
  review_store_unavailable: "复盘存储暂时不可用，请稍后重试。",
  weekly_review_unavailable: "Server 未启用周报服务。",
  review_schedule_disabled: "日报调度未启用。",
  weekly_review_schedule_disabled: "周报调度未启用。",
  review_schedule_busy: "复盘调度正忙，请稍后重试。",
  review_day_not_failed: "该日期没有处于失败状态的日报。",
  review_week_not_failed: "该周次没有处于失败状态的周报。",
  retry_request_conflict: "该重试编号已用于其他周次。",
  review_delivery_not_found: "这条报告没有投递记录。",
  review_delivery_not_retryable: "当前投递状态不允许重新投递。",
  review_delivery_target_unavailable: "服务器未配置可用的微信接收目标。",
  review_request_too_large: "请求内容过大。",
};

export function reviewFailure(status: number, error?: string) {
  if (error && FAILURE_TEXT[error]) return FAILURE_TEXT[error];
  if (status === 500) return "Server 处理失败，请稍后重试或检查服务日志。";
  if (status === 503) return "Server 暂时不可用，请稍后重试或检查服务日志。";
  return error || `请求失败 (${status})`;
}

export async function reviewApi<T = unknown>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/knowledge/reviews${path}`, {
      signal,
      credentials: "same-origin",
      cache: "no-store",
      ...(body === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }),
    });
  } catch (reason) {
    if (signal?.aborted) throw reason;
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
    throw new RuntimeError(
      response.status,
      reviewFailure(response.status, value.error),
    );
  }
  return value as T;
}

export function listReviews(
  params: {
    type: ReviewType;
    day?: string;
    weekId?: string;
    limit?: number;
    offset?: number;
  },
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({
    type: params.type,
    limit: String(params.limit ?? 20),
    offset: String(params.offset ?? 0),
  });
  if (params.day) query.set("day", params.day);
  if (params.weekId) query.set("week_id", params.weekId);
  return reviewApi<ReviewList>(`?${query}`, undefined, signal);
}

export function getReview(reviewId: string, signal?: AbortSignal) {
  return reviewApi<ReviewDetail>(
    `/${encodeURIComponent(reviewId)}`,
    undefined,
    signal,
  );
}

export function createDailyReview(input: {
  date: string;
  timezone: string;
  requestId: string;
  regenerate: boolean;
}) {
  return reviewApi<ReviewSummary>("/daily", {
    date: input.date,
    timezone: input.timezone,
    request_id: input.requestId,
    regenerate: input.regenerate,
  });
}

export function createWeeklyReview(input: {
  weekId: string;
  timezone: string;
  requestId: string;
  regenerate: boolean;
}) {
  return reviewApi<ReviewSummary>("/weekly", {
    week_id: input.weekId,
    timezone: input.timezone,
    request_id: input.requestId,
    regenerate: input.regenerate,
  });
}

export function resendDelivery(
  reviewId: string,
  input: { requestId: string; rebind: boolean },
) {
  return reviewApi<{
    delivery: Delivery;
    request_id: string;
    replayed: boolean;
  }>(`/${encodeURIComponent(reviewId)}/delivery/resend`, {
    request_id: input.requestId,
    rebind: input.rebind,
  });
}
