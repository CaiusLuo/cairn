import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { newRequestId } from "../ids";
import { knowledgeApi, type Clip } from "../knowledge-api";
import { RuntimeError } from "../runtime-api";
import { dateLabel, sourceLabel } from "./data";
import { Drawer, Markdown } from "./evidence";
import { Empty, ErrorText, PageHeader } from "./execution";
import { Icon } from "./Icon";

const CLIP_LIMIT = 100_000;
const PAGE_SIZE = 50;
const PENDING_KEY = "cairn.web.pending.clip";

type PendingSave = {
  request_id: string;
  content: string;
  source: string;
  tags: string[];
  created_at: string;
};

function splitTags(value: string) {
  return [...new Set(value.split(/[,，\n]/).map(tag => tag.trim()).filter(Boolean))];
}

function sameTags(left: string[], right: string[]) {
  return left.length === right.length && left.every((tag, index) => tag === right[index]);
}

function isPendingSave(value: unknown): value is PendingSave {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return typeof item.request_id === "string" && item.request_id.length > 0
    && typeof item.content === "string" && item.content.length > 0
    && typeof item.source === "string"
    && Array.isArray(item.tags) && item.tags.every(tag => typeof tag === "string")
    && typeof item.created_at === "string";
}

function readPending(): PendingSave | null {
  try {
    const raw = localStorage.getItem(PENDING_KEY);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (!isPendingSave(value)) { localStorage.removeItem(PENDING_KEY); return null; }
    return value;
  } catch {
    return null;
  }
}

function storePending(pending: PendingSave) {
  try { localStorage.setItem(PENDING_KEY, JSON.stringify(pending)); } catch { /* best effort */ }
}

function clearStoredPending(requestId?: string) {
  try {
    const current = readPending();
    if (requestId === undefined || current?.request_id === requestId) localStorage.removeItem(PENDING_KEY);
  } catch { /* best effort */ }
}

function submissionBody(pending: PendingSave) {
  return { request_id: pending.request_id, content: pending.content, source: pending.source, tags: pending.tags };
}

function shouldKeepPending(reason: unknown) {
  if (!(reason instanceof RuntimeError)) return true;
  return reason.status === 0 || reason.status === 401 || reason.status === 403 || reason.status >= 500;
}

function clipTitle(content: string, limit = 56) {
  const line = content.split(/\r?\n/, 1)[0].replace(/\s+/g, " ").trim();
  if (!line) return "空白片段";
  return line.length > limit ? line.slice(0, limit).trimEnd() + "…" : line;
}

function clipPreview(content: string, limit = 280) {
  const text = content.replace(/\s+/g, " ").trim();
  return text.length > limit ? text.slice(0, limit).trimEnd() + "…" : text;
}

function messageOf(reason: unknown) {
  return reason instanceof Error ? reason.message : String(reason);
}

function pagePath(query: string, showArchived: boolean, offset: number) {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
  if (query.trim()) params.set("query", query.trim());
  if (showArchived) params.set("include_archived", "1");
  return `clips?${params}`;
}

export function Knowledge() {
  const [clips, setClips] = useState<Clip[]>([]);
  const [query, setQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [listError, setListError] = useState("");
  const [revision, setRevision] = useState(0);
  const [draft, setDraft] = useState("");
  const [draftTags, setDraftTags] = useState("");
  const [saving, setSaving] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [pending, setPending] = useState<PendingSave | null>(() => readPending());
  const [composeError, setComposeError] = useState("");
  const [detail, setDetail] = useState<Clip | null>(null);
  const [editing, setEditing] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [editTags, setEditTags] = useState("");
  const [mutating, setMutating] = useState(false);
  const [detailError, setDetailError] = useState("");
  const loadEpoch = useRef(0);
  const nextOffset = useRef(0);

  useEffect(() => {
    const epoch = ++loadEpoch.current;
    const controller = new AbortController();
    setLoading(true);
    setListError("");
    setClips([]);
    setHasMore(false);
    nextOffset.current = 0;
    const timer = setTimeout(async () => {
      try {
        const data = await knowledgeApi<{ clips: Clip[] }>(pagePath(query, showArchived, 0), undefined, controller.signal);
        if (epoch !== loadEpoch.current) return;
        setClips(data.clips);
        nextOffset.current = data.clips.length;
        setHasMore(data.clips.length === PAGE_SIZE);
      } catch (reason) {
        if (epoch === loadEpoch.current && !controller.signal.aborted) setListError(messageOf(reason));
      } finally {
        if (epoch === loadEpoch.current) setLoading(false);
      }
    }, 180);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [query, showArchived, revision]);

  const refresh = () => setRevision(value => value + 1);

  async function loadMore() {
    if (loading || loadingMore || !hasMore) return;
    const epoch = loadEpoch.current;
    const offset = nextOffset.current;
    setLoadingMore(true);
    setListError("");
    try {
      const data = await knowledgeApi<{ clips: Clip[] }>(pagePath(query, showArchived, offset));
      if (epoch !== loadEpoch.current) return;
      setClips(items => {
        const seen = new Set(items.map(item => item.clip_id));
        return [...items, ...data.clips.filter(item => !seen.has(item.clip_id))];
      });
      nextOffset.current = offset + data.clips.length;
      setHasMore(data.clips.length === PAGE_SIZE);
    } catch (reason) {
      if (epoch === loadEpoch.current) setListError(messageOf(reason));
    } finally {
      if (epoch === loadEpoch.current) setLoadingMore(false);
    }
  }

  const pendingMatchesDraft = !!pending
    && pending.source === "web"
    && pending.content === draft
    && sameTags(pending.tags, splitTags(draftTags));

  async function submitSave(submission: PendingSave) {
    setSaving(true);
    setComposeError("");
    storePending(submission);
    try {
      await knowledgeApi<Clip>("clips", submissionBody(submission));
      clearStoredPending(submission.request_id);
      setPending(current => current?.request_id === submission.request_id ? null : current);
      setDraft("");
      setDraftTags("");
      refresh();
    } catch (reason) {
      if (shouldKeepPending(reason)) {
        setPending(submission);
      } else {
        clearStoredPending(submission.request_id);
        setPending(current => current?.request_id === submission.request_id ? null : current);
      }
      setComposeError(messageOf(reason));
    } finally {
      setSaving(false);
    }
  }

  async function retryPending() {
    if (!pending || retrying || saving) return;
    const submission = pending;
    setRetrying(true);
    setComposeError("");
    try {
      await knowledgeApi<Clip>("clips", submissionBody(submission));
      clearStoredPending(submission.request_id);
      setPending(null);
      if (pendingMatchesDraft) { setDraft(""); setDraftTags(""); }
      refresh();
    } catch (reason) {
      if (!shouldKeepPending(reason)) {
        clearStoredPending(submission.request_id);
        setPending(null);
      }
      setComposeError(messageOf(reason));
    } finally {
      setRetrying(false);
    }
  }

  function discardPending() {
    if (!window.confirm("放弃恢复上次保存？如果 Server 已经提交，片段仍会出现在列表中。")) return;
    clearStoredPending(pending?.request_id);
    setPending(null);
    setComposeError("");
  }

  async function saveClip() {
    if (!draft.trim() || draft.length > CLIP_LIMIT || saving || retrying) return;
    if (pendingMatchesDraft) { await retryPending(); return; }
    await submitSave({
      request_id: newRequestId(),
      content: draft,
      source: "web",
      tags: splitTags(draftTags),
      created_at: new Date().toISOString(),
    });
  }

  function composeKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void saveClip();
    }
  }

  function openClip(clip: Clip) {
    setDetail(clip);
    setEditing(false);
    setPreviewing(false);
    setDetailError("");
    setEditContent(clip.content);
    setEditTags(clip.tags.join(", "));
  }

  function replaceClip(clip: Clip) {
    setDetail(current => current?.clip_id === clip.clip_id ? clip : current);
    setClips(items => items.map(item => item.clip_id === clip.clip_id ? clip : item));
  }

  async function saveEdit() {
    if (!detail || mutating || !editContent.trim()) return;
    setMutating(true);
    setDetailError("");
    try {
      const updated = await knowledgeApi<Clip>(`clips/${encodeURIComponent(detail.clip_id)}/update`, {
        content: editContent,
        tags: splitTags(editTags),
      });
      replaceClip(updated);
      setEditing(false);
      refresh();
    } catch (reason) {
      setDetailError(messageOf(reason));
    } finally {
      setMutating(false);
    }
  }

  async function toggleArchived(clip: Clip) {
    if (mutating) return;
    setMutating(true);
    setDetailError("");
    try {
      const updated = await knowledgeApi<Clip>(`clips/${encodeURIComponent(clip.clip_id)}/${clip.archived_at ? "unarchive" : "archive"}`, {});
      replaceClip(updated);
      refresh();
    } catch (reason) {
      if (detail?.clip_id === clip.clip_id) setDetailError(messageOf(reason));
      else setListError(messageOf(reason));
    } finally {
      setMutating(false);
    }
  }

  const saveLabel = saving ? "保存中…" : retrying ? "恢复中…" : pendingMatchesDraft ? "重试上次保存" : "保存片段";

  return <div className="wb-page">
    <PageHeader icon="doc" title="知识" description="粘贴原文或 Markdown，保留可搜索的私有片段"/>
    {pending && <section className="wb-card knowledge-pending" role="status">
      <div>
        <strong>上次保存尚未确认</strong>
        <p>{clipTitle(pending.content, 40)}{pending.tags.length ? ` · ${pending.tags.join("、")}` : ""}</p>
        <small className="wb-muted">{dateLabel(pending.created_at)} · 重试会复用同一请求身份，不会重复保存。</small>
      </div>
      <div className="knowledge-pending-actions">
        <button disabled={retrying || saving} onClick={() => void retryPending()}>{retrying ? "恢复中…" : "重试保存"}</button>
        <button disabled={retrying || saving} onClick={discardPending}>放弃恢复</button>
      </div>
    </section>}
    <section className="wb-card knowledge-compose">
      <textarea
        aria-label="片段原文"
        maxLength={CLIP_LIMIT}
        placeholder="粘贴文本或 Markdown…（⌘/Ctrl + Enter 保存）"
        value={draft}
        onChange={event => setDraft(event.target.value)}
        onKeyDown={composeKey}
      />
      <div className="knowledge-compose-row">
        <label><Icon name="doc" size={14}/><input aria-label="片段标签" placeholder="标签，用逗号分隔（可选）" value={draftTags} onChange={event => setDraftTags(event.target.value)}/></label>
        <span className="knowledge-count">{draft.length.toLocaleString("zh-CN")} / {CLIP_LIMIT.toLocaleString("zh-CN")}</span>
        <button disabled={saving || retrying || !draft.trim() || draft.length > CLIP_LIMIT} onClick={() => void saveClip()}><Icon name="plus" size={15}/>{saveLabel}</button>
      </div>
      <ErrorText value={composeError}/>
    </section>
    <div className="wb-toolbar">
      <label><Icon name="search" size={15}/><input type="search" aria-label="搜索知识片段" placeholder="搜索内容或标签" value={query} onChange={event => setQuery(event.target.value)}/></label>
      <label className="knowledge-archive-toggle"><input type="checkbox" checked={showArchived} onChange={event => setShowArchived(event.target.checked)}/>显示已归档</label>
    </div>
    <ErrorText value={listError}/>
    {loading && !clips.length ? <p className="wb-muted" role="status">正在读取知识片段…</p> : null}
    {clips.length ? <>
      <div className="knowledge-grid">{clips.map(clip => <article className={`knowledge-card ${clip.archived_at ? "is-archived" : ""}`} key={clip.clip_id}>
        <button className="knowledge-card-main" onClick={() => openClip(clip)}>
          <span className="knowledge-meta"><span>{sourceLabel(clip.source)}</span><time>{dateLabel(clip.updated_at)}</time></span>
          <h2>{clipTitle(clip.content)}</h2>
          <p className="knowledge-preview">{clipPreview(clip.content)}</p>
          {!!clip.tags.length && <span className="knowledge-tags">{clip.tags.slice(0, 4).map(tag => <span className="knowledge-tag" key={tag}>{tag}</span>)}{clip.tags.length > 4 && <span className="knowledge-tag">+{clip.tags.length - 4}</span>}</span>}
        </button>
        <footer>
          <small>{clip.archived_at ? "已归档" : `更新于 ${dateLabel(clip.updated_at)}`}</small>
          <button disabled={mutating} onClick={() => void toggleArchived(clip)}>{clip.archived_at ? "恢复" : "归档"}</button>
        </footer>
      </article>)}</div>
      {hasMore && <div className="knowledge-more"><button disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "加载中…" : "加载更多"}</button></div>}
    </> : !loading && !listError ? <Empty icon="doc" text={query ? "没有匹配的知识片段。" : "还没有保存的知识片段。"}/> : null}
    {detail && <Drawer title="知识片段" close={() => setDetail(null)}>
      <div className="knowledge-detail">
        <div className="knowledge-detail-meta">
          <span>{sourceLabel(detail.source)}</span>
          <span>创建 {dateLabel(detail.created_at)}</span>
          <span>更新 {dateLabel(detail.updated_at)}</span>
          {detail.archived_at && <span>已归档</span>}
        </div>
        <ErrorText value={detailError}/>
        {editing ? <form className="knowledge-edit" onSubmit={event => { event.preventDefault(); void saveEdit(); }}>
          <label>片段原文<textarea aria-label="编辑片段内容" maxLength={CLIP_LIMIT} value={editContent} onChange={event => setEditContent(event.target.value)}/></label>
          <label>标签<input aria-label="编辑片段标签" value={editTags} onChange={event => setEditTags(event.target.value)}/></label>
          <div className="knowledge-detail-actions">
            <button type="submit" disabled={mutating || !editContent.trim()}>{mutating ? "保存中…" : "保存修改"}</button>
            <button type="button" disabled={mutating} onClick={() => setEditing(false)}>取消</button>
          </div>
        </form> : <>
          <div className="knowledge-detail-actions">
            <button onClick={() => { setEditing(true); setPreviewing(false); }}>编辑内容</button>
            <button onClick={() => setPreviewing(value => !value)}>{previewing ? "显示原文" : "预览 Markdown"}</button>
            <button disabled={mutating} onClick={() => void toggleArchived(detail)}>{detail.archived_at ? "恢复片段" : "归档片段"}</button>
          </div>
          {previewing ? <Markdown text={detail.content}/> : <pre className="knowledge-content">{detail.content}</pre>}
          {!!detail.tags.length && <div className="knowledge-tags">{detail.tags.map(tag => <span className="knowledge-tag" key={tag}>{tag}</span>)}</div>}
          <p className="knowledge-hash">SHA-256 · {detail.content_hash}</p>
          <p className="wb-muted">片段只保存在私有知识库，不会自动加入对话、系统提示或 Agent 记忆。</p>
        </>}
      </div>
    </Drawer>}
  </div>;
}
