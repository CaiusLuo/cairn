import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { newRequestId } from "../ids";
import { knowledgeApi, type Clip } from "../knowledge-api";
import { dateLabel, sourceLabel } from "./data";
import { Drawer, Markdown } from "./evidence";
import { Empty, ErrorText, PageHeader } from "./execution";
import { Icon } from "./Icon";

const CLIP_LIMIT = 100_000;

function splitTags(value: string) {
  return [...new Set(value.split(/[,，\n]/).map(tag => tag.trim()).filter(Boolean))];
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

export function Knowledge() {
  const [clips, setClips] = useState<Clip[]>([]);
  const [query, setQuery] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [revision, setRevision] = useState(0);
  const [draft, setDraft] = useState("");
  const [draftTags, setDraftTags] = useState("");
  const [saving, setSaving] = useState(false);
  const [composeError, setComposeError] = useState("");
  const [detail, setDetail] = useState<Clip | null>(null);
  const [editing, setEditing] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [editTags, setEditTags] = useState("");
  const [mutating, setMutating] = useState(false);
  const [detailError, setDetailError] = useState("");
  const requestId = useRef(newRequestId());

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setLoading(true);
      setListError("");
      const params = new URLSearchParams({ limit: "100" });
      if (query.trim()) params.set("query", query.trim());
      if (showArchived) params.set("include_archived", "1");
      try {
        const data = await knowledgeApi<{ clips: Clip[] }>(`clips?${params}`, undefined, controller.signal);
        if (!controller.signal.aborted) setClips(data.clips);
      } catch (reason) {
        if (!controller.signal.aborted) setListError(messageOf(reason));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, 180);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [query, showArchived, revision]);

  const refresh = () => setRevision(value => value + 1);

  function changeDraft(value: string) {
    setDraft(value);
    if (!saving) requestId.current = newRequestId();
  }

  async function saveClip() {
    if (!draft.trim() || draft.length > CLIP_LIMIT || saving) return;
    setSaving(true);
    setComposeError("");
    try {
      await knowledgeApi<Clip>("clips", {
        content: draft,
        source: "web",
        tags: splitTags(draftTags),
        request_id: requestId.current,
      });
      setDraft("");
      setDraftTags("");
      requestId.current = newRequestId();
      refresh();
    } catch (reason) {
      setComposeError(messageOf(reason));
    } finally {
      setSaving(false);
    }
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

  return <div className="wb-page">
    <PageHeader icon="doc" title="知识" description="粘贴原文或 Markdown，保留可搜索的私有片段"/>
    <section className="wb-card knowledge-compose">
      <textarea
        aria-label="片段原文"
        maxLength={CLIP_LIMIT}
        placeholder="粘贴文本或 Markdown…（⌘/Ctrl + Enter 保存）"
        value={draft}
        onChange={event => changeDraft(event.target.value)}
        onKeyDown={composeKey}
      />
      <div className="knowledge-compose-row">
        <label><Icon name="doc" size={14}/><input aria-label="片段标签" placeholder="标签，用逗号分隔（可选）" value={draftTags} onChange={event => setDraftTags(event.target.value)}/></label>
        <span className="knowledge-count">{draft.length.toLocaleString("zh-CN")} / {CLIP_LIMIT.toLocaleString("zh-CN")}</span>
        <button disabled={saving || !draft.trim() || draft.length > CLIP_LIMIT} onClick={() => void saveClip()}><Icon name="plus" size={15}/>{saving ? "保存中…" : "保存片段"}</button>
      </div>
      <ErrorText value={composeError}/>
    </section>
    <div className="wb-toolbar">
      <label><Icon name="search" size={15}/><input type="search" aria-label="搜索知识片段" placeholder="搜索内容或标签" value={query} onChange={event => setQuery(event.target.value)}/></label>
      <label className="knowledge-archive-toggle"><input type="checkbox" checked={showArchived} onChange={event => setShowArchived(event.target.checked)}/>显示已归档</label>
    </div>
    <ErrorText value={listError}/>
    {loading && !clips.length ? <p className="wb-muted" role="status">正在读取知识片段…</p> : null}
    {clips.length ? <div className="knowledge-grid">{clips.map(clip => <article className={`knowledge-card ${clip.archived_at ? "is-archived" : ""}`} key={clip.clip_id}>
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
    </article>)}</div> : !loading && !listError ? <Empty icon="doc" text={query ? "没有匹配的知识片段。" : "还没有保存的知识片段。"}/> : null}
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
