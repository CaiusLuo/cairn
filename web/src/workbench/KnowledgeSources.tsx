import { lazy, Suspense, useEffect, useState } from "react";
import { documentChunk, type DocumentChunk } from "../documents-api";
import type { KnowledgeCard, KnowledgeSource } from "./data";
import { Drawer } from "./evidence";
import { Icon } from "./Icon";

type SourcesListProps = {
  sources: KnowledgeSource[];
  onOpen: (source: KnowledgeSource) => void;
};

export function SourcesList({ sources, onOpen }: SourcesListProps) {
  return <section className="wb-knowledge-sources" aria-label="知识库检索来源">
    <h3><Icon name="doc" size={15}/>知识库检索来源</h3>
    <ul role="list">{sources.map(source => <li key={source.chunk_id}>
      <div>
        <strong>{source.title ?? "来源已不可用"}</strong>
        {source.heading && <small>{source.heading}</small>}
      </div>
      {source.title && <button type="button" aria-label={"查看片段：" + source.title} onClick={() => onOpen(source)}>查看片段</button>}
    </li>)}</ul>
  </section>;
}

const Surface = lazy(() => import("./KnowledgeSurface").catch(() => ({ default: SourcesList })));

export function KnowledgeSources({ card, onAsk }: {
  card: KnowledgeCard;
  onAsk: (source: KnowledgeSource & { offset: number }) => void;
}) {
  const [selected, setSelected] = useState<KnowledgeSource | null>(null);
  return <>
    <Suspense fallback={<p className="wb-muted">正在载入知识库来源…</p>}>
      <Surface runId={card.run_id} sources={card.sources} onOpen={setSelected}/>
    </Suspense>
    {selected && <SourcePreview key={selected.chunk_id} source={selected} close={() => setSelected(null)} onAsk={onAsk}/>}
  </>;
}

function SourcePreview({ source, close, onAsk }: {
  source: KnowledgeSource;
  close: () => void;
  onAsk: (source: KnowledgeSource & { offset: number }) => void;
}) {
  const [preview, setPreview] = useState<DocumentChunk>();
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void documentChunk(source.document_id, source.chunk_id, controller.signal).then(
      chunk => { if (!controller.signal.aborted) setPreview(chunk); },
      reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : String(reason)); },
    );
    return () => controller.abort();
  }, [source.document_id, source.chunk_id]);

  return <Drawer title={preview?.title ?? source.title ?? "知识库片段"} close={close}>
    <div className="wb-source-preview">
      {error && <p className="wb-error" role="alert">{error}</p>}
      {!preview && !error && <p className="wb-muted">正在读取片段…</p>}
      {preview && <>
        {preview.heading && <p className="wb-muted">{preview.heading}</p>}
        <pre>{preview.content}</pre>
        <button type="button" onClick={event => {
          event.currentTarget.closest("dialog")?.close();
          onAsk({ ...source, title: preview.title, offset: preview.offset });
        }}>用此片段追问</button>
      </>}
    </div>
  </Drawer>;
}
