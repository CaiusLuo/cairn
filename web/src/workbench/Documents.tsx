import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteDocument,
  documentText,
  listDocuments,
  retryDocument,
  uploadDocument,
  type KnowledgeDocument,
} from "../documents-api";
import { dateLabel } from "./data";
import { Drawer } from "./evidence";
import { Empty, ErrorText } from "./execution";
import { Icon } from "./Icon";

const POLL_MS = 2000;
const PAGE_SIZE = 50;
const ACCEPT = ".pdf,.docx,.xlsx,.xlsm,.pptx,.html,.htm,.md,.markdown,.txt,.csv";
const STATUS_LABEL: Record<string, string> = {
  pending: "待处理",
  processing: "处理中",
  ready: "可搜索",
  failed: "失败",
};

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function isActive(document: KnowledgeDocument) {
  return document.status === "pending" || document.status === "processing";
}

function messageOf(reason: unknown) {
  return reason instanceof Error ? reason.message : String(reason);
}

export function Documents() {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [uploading, setUploading] = useState<string[]>([]);
  const [uploadError, setUploadError] = useState("");
  const [busyId, setBusyId] = useState("");
  const [revision, setRevision] = useState(0);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [detail, setDetail] = useState<KnowledgeDocument | null>(null);
  const [content, setContent] = useState("");
  const [totalChars, setTotalChars] = useState(0);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const uploadInput = useRef<HTMLInputElement>(null);
  const previewRequest = useRef<AbortController | null>(null);
  const detailId = detail?.document_id;
  const detailStatus = detail?.status;

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    let polling = false;
    const controller = new AbortController();
    async function load() {
      try {
        const { documents: page } = await listDocuments(PAGE_SIZE + 1, offset, controller.signal);
        if (!alive) return;
        if (!page.length && offset > 0) {
          setOffset(offset - PAGE_SIZE);
          return;
        }
        const visible = page.slice(0, PAGE_SIZE);
        setDocuments(visible);
        setHasMore(page.length > PAGE_SIZE);
        setDetail(current => current ? visible.find(item => item.document_id === current.document_id) ?? current : null);
        setListError("");
        polling = visible.some(isActive);
        if (polling) timer = window.setTimeout(() => void load(), POLL_MS);
      } catch (reason) {
        if (!alive || controller.signal.aborted) return;
        setListError(messageOf(reason));
        if (polling) timer = window.setTimeout(() => void load(), POLL_MS);
      } finally {
        if (alive) setLoading(false);
      }
    }
    setLoading(true);
    void load();
    return () => {
      alive = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [offset, revision]);

  const refresh = () => setRevision(value => value + 1);

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    const selected = [...files];
    setUploadError("");
    setUploading(selected.map(file => file.name));
    try {
      for (const file of selected) {
        await uploadDocument(file);
      }
    } catch (reason) {
      setUploadError(messageOf(reason));
    } finally {
      setOffset(0);
      refresh();
      setUploading([]);
      if (uploadInput.current) uploadInput.current.value = "";
    }
  }

  const loadText = useCallback(async (id: string, offset: number, signal: AbortSignal) => {
    setDetailError("");
    setDetailLoading(true);
    try {
      const page = await documentText(id, offset, 20_000, signal);
      if (signal.aborted) return;
      setContent(current => offset === 0 ? page.content : current + page.content);
      setTotalChars(page.total_chars);
      setNextOffset(page.next_offset);
    } catch (reason) {
      if (!signal.aborted) setDetailError(messageOf(reason));
    } finally {
      if (!signal.aborted) setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    setContent("");
    setTotalChars(0);
    setNextOffset(null);
    setDetailError("");
    setDetailLoading(false);
    if (!detailId || detailStatus !== "ready") return;
    const controller = new AbortController();
    previewRequest.current = controller;
    void loadText(detailId, 0, controller.signal);
    return () => {
      controller.abort();
      previewRequest.current = null;
    };
  }, [detailId, detailStatus, loadText]);

  function openDocument(document: KnowledgeDocument) {
    if (document.document_id === detailId) return;
    previewRequest.current?.abort();
    setContent("");
    setDetailError("");
    setNextOffset(null);
    setDetail(document);
  }

  function closeDocument() {
    previewRequest.current?.abort();
    setDetail(null);
  }

  function moreText() {
    const signal = previewRequest.current?.signal;
    if (detailId && nextOffset !== null && signal && !signal.aborted) {
      void loadText(detailId, nextOffset, signal);
    }
  }

  async function retry(document: KnowledgeDocument) {
    if (busyId) return;
    setBusyId(document.document_id);
    setListError("");
    try {
      const updated = await retryDocument(document.document_id);
      setDocuments(items => items.map(item => item.document_id === updated.document.document_id ? updated.document : item));
      setDetail(current => current?.document_id === updated.document.document_id ? updated.document : current);
      refresh();
    } catch (reason) {
      setListError(messageOf(reason));
    } finally {
      setBusyId("");
    }
  }

  async function remove(document: KnowledgeDocument) {
    if (busyId || !window.confirm(`删除「${document.title}」及其索引？原文件也会被删除。`)) return;
    setBusyId(document.document_id);
    setListError("");
    try {
      await deleteDocument(document.document_id);
      setDetail(current => current?.document_id === document.document_id ? null : current);
      refresh();
    } catch (reason) {
      setListError(messageOf(reason));
    } finally {
      setBusyId("");
    }
  }

  return <>
    <section className="wb-card documents-upload">
      <div>
        <strong>上传文档</strong>
        <p className="wb-muted">PDF、Word、Excel、PowerPoint、HTML、Markdown 或纯文本，单个文件不超过 25 MiB。上传后会自动提取文本、建立可搜索索引，并尽力生成实体关系。</p>
      </div>
      <input ref={uploadInput} type="file" multiple accept={ACCEPT} aria-label="选择要上传的文档" onChange={event => void upload(event.target.files)} hidden/>
      <button disabled={!!uploading.length} onClick={() => uploadInput.current?.click()}><Icon name="plus" size={15}/>{uploading.length ? "上传中…" : "选择文件"}</button>
    </section>
    {!!uploading.length && <p className="wb-muted" role="status">正在上传 {uploading.join("、")}…</p>}
    <ErrorText value={uploadError}/>
    <ErrorText value={listError}/>
    {loading && !documents.length ? <p className="wb-muted" role="status">正在读取文档库…</p> : null}
    {documents.length ? <div className="documents-list">{documents.map(document => <article className="wb-card documents-row" key={document.document_id}>
      <button className="documents-main" onClick={() => openDocument(document)}>
        <span className="documents-title"><Icon name="doc" size={16}/><strong>{document.title}</strong></span>
        <span className={`documents-status is-${document.status}`}>{STATUS_LABEL[document.status] || document.status}</span>
        <span className="documents-meta">{formatBytes(document.size_bytes)} · 上传于 {dateLabel(document.created_at)}</span>
        {document.status === "ready" && <span className="documents-meta">向量 {document.embedding_status} · 图谱 {document.graph_status}</span>}
        {document.error && <span className="documents-error">{document.error}</span>}
      </button>
      <footer>
        <button disabled={!!busyId || isActive(document)} onClick={() => void retry(document)}>重新处理</button>
        <button disabled={busyId === document.document_id} onClick={() => void remove(document)}>删除</button>
      </footer>
    </article>)}</div> : !loading && !listError ? <Empty icon="folder" text="还没有上传文档。"/> : null}
    {(offset > 0 || hasMore) && <nav className="knowledge-more" aria-label="文档列表分页">
      <button disabled={loading || offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>上一页</button>
      <button disabled={loading || !hasMore} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button>
    </nav>}
    {detail && <Drawer title={detail.title} close={closeDocument}>
      <div className="knowledge-detail">
        <div className="knowledge-detail-meta">
          <span>{STATUS_LABEL[detail.status] || detail.status}</span>
          <span>{formatBytes(detail.size_bytes)}</span>
          <span>上传 {dateLabel(detail.created_at)}</span>
          {detail.processed_at && <span>处理 {dateLabel(detail.processed_at)}</span>}
          {detail.status === "ready" && <span>向量 {detail.embedding_status} · 图谱 {detail.graph_status}</span>}
        </div>
        <ErrorText value={detailError || detail.error || ""}/>
        <div className="knowledge-detail-actions">
          <button disabled={!!busyId || isActive(detail)} onClick={() => void retry(detail)}>重新处理</button>
          <button disabled={busyId === detail.document_id} onClick={() => void remove(detail)}>删除文档</button>
        </div>
        {detailLoading && !content ? <p className="wb-muted" role="status">正在读取文本…</p> : content ? <>
          <pre className="knowledge-content">{content}</pre>
          <p className="wb-muted">{(nextOffset ?? totalChars).toLocaleString("zh-CN")} / {totalChars.toLocaleString("zh-CN")} 字符</p>
          {nextOffset !== null && <div className="knowledge-more"><button disabled={detailLoading} onClick={moreText}>{detailLoading ? "加载中…" : "加载更多"}</button></div>}
        </> : !detailLoading && detail.status === "ready" ? <p className="wb-muted">没有可显示的文本。</p> : <p className="wb-muted">文档处理完成后可查看提取的文本。</p>}
        <p className="wb-muted">文档内容只是不可信的参考材料；对话中引用时会带上 document_id 和 chunk_id。</p>
      </div>
    </Drawer>}
  </>;
}
