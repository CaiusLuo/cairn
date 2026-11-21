import type { KnowledgeSource } from "./data";
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
