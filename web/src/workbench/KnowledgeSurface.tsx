import { createContext, useContext, useMemo } from "react";
import { A2uiSurface, createComponentImplementation, type ReactCatalogComponent } from "@a2ui/react";
import { Catalog, MessageProcessor, type SurfaceModel } from "@a2ui/web_core";
import { z } from "zod";
import type { KnowledgeSource } from "./data";
import { SourcesList } from "./KnowledgeSourceList";

const sourcesSchema = z.array(z.object({
  document_id: z.string().regex(/^doc-[0-9a-f]{32}$/),
  chunk_id: z.number().int().positive().safe(),
  title: z.string().max(255).nullable(),
  heading: z.string().max(972),
}).strict()).min(1).max(3);

const OpenSource = createContext<(source: KnowledgeSource) => void>(() => {});

const SourcesComponent = createComponentImplementation({
  name: "KnowledgeSources",
  schema: z.object({ sources: sourcesSchema }),
}, function Sources({ props }) {
  return <SourcesList sources={props.sources} onOpen={useContext(OpenSource)}/>;
});

const catalog = new Catalog<ReactCatalogComponent>(
  "https://cairn.local/a2ui/knowledge/v1",
  "v1.0",
  [SourcesComponent],
  [],
);

export default function KnowledgeSurface({ runId, sources, onOpen }: {
  runId: string;
  sources: KnowledgeSource[];
  onOpen: (source: KnowledgeSource) => void;
}) {
  const surface = useMemo<SurfaceModel<ReactCatalogComponent> | undefined>(() => {
    const data = sourcesSchema.safeParse(sources);
    if (!data.success) return undefined;
    const processor = new MessageProcessor<ReactCatalogComponent>([catalog]);
    try {
      processor.processMessages({
        version: "v1.0",
        createSurface: {
          surfaceId: runId,
          catalogId: catalog.id,
          components: [{ id: "root", component: "KnowledgeSources", sources: data.data }],
        },
      });
      return processor.model.surfacesMap.get(runId);
    } catch {
      return undefined;
    }
  }, [runId, sources]);
  if (!surface) return <SourcesList sources={sources} onOpen={onOpen}/>;
  return <OpenSource.Provider value={onOpen}><A2uiSurface surface={surface}/></OpenSource.Provider>;
}
