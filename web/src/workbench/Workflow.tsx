import { useMemo, useRef, useState } from "react";
import { Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, type Edge, type Node, type NodeProps, type ReactFlowInstance, type XYPosition } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { isActive, type ExecutionEvent, type Task } from "./data";
import { Badge } from "./evidence";
import { Empty, ErrorText, PageHeader, useEventLog } from "./execution";
import { Icon } from "./Icon";
import { href } from "./Shell";

const stages: Record<string, { label: string; icon: "board" | "terminal" | "runs" | "folder" | "check" | "gate" }> = {
  accepted: { label: "接收目标", icon: "board" },
  running: { label: "开始执行", icon: "runs" },
  worktree_ready: { label: "准备工作树", icon: "folder" },
  tool_result: { label: "工具结果", icon: "terminal" },
  completed: { label: "执行完成", icon: "check" },
  failed: { label: "执行失败", icon: "gate" },
  paused: { label: "执行暂停", icon: "gate" },
  cancelled: { label: "执行取消", icon: "gate" },
  recovery_required: { label: "等待恢复核对", icon: "gate" },
  interrupted: { label: "执行中断", icon: "gate" },
  reconciled: { label: "已记录人工核对", icon: "gate" },
};

type Stage = Node<{ event: ExecutionEvent; label: string; icon: (typeof stages)[string]["icon"] }, "stage">;
type Layout = Record<string, XYPosition>;

function StageNode({ data, selected }: NodeProps<Stage>) {
  return <div className={`flow-node ${selected ? "selected" : ""}`}>
    <Handle type="target" position={Position.Left} isConnectable={false}/>
    <span className="task-symbol"><Icon name={data.icon} size={16}/></span>
    <div><strong>{data.label}</strong><small>记录 #{data.event.sequence}</small></div>
    <Handle type="source" position={Position.Right} isConnectable={false}/>
  </div>;
}
const nodeTypes = { stage: StageNode };
const mobileViewport = { x: 0, y: 48, zoom: 0.85 };
const canvasLabels = {
  "controls.ariaLabel": "画布视图",
  "controls.zoomIn.ariaLabel": "放大画布",
  "controls.zoomOut.ariaLabel": "缩小画布",
  "controls.fitView.ariaLabel": "总览流程",
};

export function Workflow({ tasks, selected, navigate, error }: { tasks: Task[]; selected: string; navigate: (path: string) => void; error?: string }) {
  const task = selected ? tasks.find(item => item.task_id === selected) : tasks[0];
  return <div className="wb-page wb-flow-page">
    <PageHeader icon="workflow" title="流程" description="查看开发任务记录。连线表示事件先后顺序。" actions={
      <select aria-label="选择流程任务" value={task?.task_id ?? selected} onChange={event => navigate(href("workflow", { task: event.target.value }))}>
        <option value="" disabled>选择开发任务</option>
        {selected && !task && <option value={selected}>{selected}</option>}
        {tasks.map(item => <option key={item.task_id} value={item.task_id}>{item.prompt.slice(0, 60)}</option>)}
      </select>
    }/>
    <ErrorText value={error}/>
    {task ? <TaskFlow key={task.task_id} task={task}/> : <Empty icon="workflow" text={selected ? "正在读取所选开发任务…" : "创建开发任务后，可查看 Server 记录的执行链路。"}/>}
  </div>;
}

function TaskFlow({ task }: { task: Task }) {
  const log = useEventLog(`tasks/${encodeURIComponent(task.task_id)}`, isActive(task.status));
  const events = log.rows.filter(event => event.kind in stages).slice(-35);
  const [layout, setLayout] = useState<Layout>({});
  const [drag, setDrag] = useState<Layout>({});
  const [undo, setUndo] = useState<Layout[]>([]);
  const [redo, setRedo] = useState<Layout[]>([]);
  const [selected, setSelected] = useState<string>();
  const graph = useRef<ReactFlowInstance<Stage, Edge> | null>(null);
  const defaults = useMemo(() => Object.fromEntries(events.map((event, index) => [String(event.sequence), { x: 30 + index * 236, y: 100 }])), [events]);
  const nodes: Stage[] = events.map(event => {
    const id = String(event.sequence);
    return { id, position: drag[id] ?? layout[id] ?? defaults[id], type: "stage", initialWidth: 180, initialHeight: 63, data: { event, ...stages[event.kind] }, selected: id === selected };
  });
  const edges: Edge[] = nodes.slice(1).map((node, index) => ({ id: nodes[index].id + "-" + node.id, source: nodes[index].id, target: node.id, markerEnd: { type: MarkerType.ArrowClosed }, type: "smoothstep" }));
  const current = nodes.find(node => node.id === selected);
  const narrow = matchMedia("(max-width: 520px)").matches;
  function fit() {
    if (narrow) void graph.current?.setViewport(mobileViewport);
    else void graph.current?.fitView({ padding: 0.24, maxZoom: 1.1 });
  }
  function remember(next: Layout) {
    setUndo(previous => [...previous, layout].slice(-40)); setRedo([]); setLayout(next);
  }
  function back() {
    const previous = undo.at(-1);
    if (!previous) return;
    setUndo(undo.slice(0, -1)); setRedo(history => [...history, layout]); setLayout(previous);
  }
  function forward() {
    const next = redo.at(-1);
    if (!next) return;
    setRedo(redo.slice(0, -1)); setUndo(history => [...history, layout]); setLayout(next);
  }

  return <><div className="flow-meta"><Badge status={task.status}/><a className="wb-inline-link" href={href("tasks", { task: task.task_id })}>打开任务详情 <Icon name="arrow-right" size={14}/></a><span>只读执行记录 · 布局调整只在当前页面有效</span></div>
    <ErrorText value={log.error}/>
    {nodes.length ? <div className="flow-workspace">
      <div className="flow-canvas" aria-label="任务流程画布">
        <ReactFlow<Stage, Edge> nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView={!narrow} defaultViewport={narrow ? mobileViewport : undefined} ariaLabelConfig={canvasLabels} fitViewOptions={{ padding: .24, maxZoom: 1.1 }} minZoom={.2} maxZoom={2} nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null}
          onInit={instance => { graph.current = instance; }} onNodeClick={(_event, node) => setSelected(node.id)} onPaneClick={() => setSelected(undefined)}
          onNodesChange={changes => {
            const position = changes.find(change => change.type === "position" && change.position);
            if (position?.type !== "position" || !position.position) return;
            if (position.dragging) setDrag({ [position.id]: position.position });
            else if (position.dragging === undefined) remember({ ...layout, [position.id]: position.position });
          }}
          onNodeDragStop={(_event, node) => { setDrag({}); const previous = layout[node.id] ?? defaults[node.id]; if (previous.x !== node.position.x || previous.y !== node.position.y) remember({ ...layout, [node.id]: node.position }); }}>
          <Background gap={18} size={1.15}/><Controls showInteractive={false}/><MiniMap pannable zoomable nodeColor="var(--hui-accent)"/>
        </ReactFlow>
        <div className="flow-actions">
          <button disabled={!undo.length} title="撤销布局" aria-label="撤销布局" onClick={back}><Icon name="undo" size={15}/></button>
          <button disabled={!redo.length} title="重做布局" aria-label="重做布局" onClick={forward}><Icon name="redo" size={15}/></button>
          <button onClick={() => { if (Object.keys(layout).length) remember({}); requestAnimationFrame(fit); }}><Icon name="layout" size={15}/> 自动布局</button>
        </div>
      </div>
      <aside className="flow-inspector">
        <h2>{current?.data.label ?? "节点详情"}</h2>
        {current ? <><p>已记录事件 #{current.data.event.sequence}</p><pre>{JSON.stringify(current.data.event.data, null, 2)}</pre></> : <p>选择节点查看 Server 事件。</p>}
        <p>依赖编辑和流程执行等待服务端契约。</p>
      </aside>
    </div> : <Empty icon="workflow" text="等待 Server 写入执行事件。"/>}
  </>;
}
