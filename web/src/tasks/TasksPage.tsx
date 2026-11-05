import { useEffect, useMemo, useState } from "react";
import {
  DndContext,
  DragOverlay,
  PointerSensor,
  closestCorners,
  useDraggable,
  useDroppable,
  type DragEndEvent,
  type DragStartEvent
} from "@dnd-kit/core";
import { Sidebar } from "../components/Sidebar";
import { SyncBadge } from "../components/SyncBadge";
import {
  TaskUpdateError,
  shortId,
  sourceClassName,
  sourceLabel,
  updateTaskStatus,
  type Snapshot,
  type TaskItem
} from "../lib";
import { useLiveSnapshot } from "../useLiveSnapshot";

type Filter = "all" | "running" | "done";

type Column = {
  key: string;
  label: string;
  statuses: string[];
  droppable: boolean;
};

const COLUMNS: Column[] = [
  { key: "backlog", label: "Backlog", statuses: ["todo"], droppable: true },
  { key: "queued", label: "Queued", statuses: ["queued"], droppable: true },
  { key: "progress", label: "In progress", statuses: ["running"], droppable: true },
  { key: "waiting", label: "Waiting", statuses: ["waiting"], droppable: true },
  { key: "done", label: "Done", statuses: ["completed"], droppable: true },
  { key: "closed", label: "Closed", statuses: ["failed", "cancelled"], droppable: false }
];

const STATUS_LABELS: Record<string, string> = {
  todo: "Backlog",
  queued: "Queued",
  running: "In progress",
  waiting: "Waiting",
  completed: "Done",
  failed: "Failed",
  cancelled: "Cancelled"
};

const STATUS_OPTIONS = ["todo", "queued", "running", "waiting", "completed", "failed", "cancelled"];

const DRAG_SENSORS = [{ sensor: PointerSensor, options: { activationConstraint: { distance: 8 } } }];


function taskTitle(task: TaskItem): string {
  return task.title.trim() || shortId(task.task_id, 12);
}

function taskCount(task: TaskItem): string {
  const done = task.todos.filter((todo) => todo.done).length;
  return `${done}/${task.todos.length}`;
}

function columnForTask(task: TaskItem): string {
  return COLUMNS.find((column) => column.statuses.includes(task.status))?.key ?? "closed";
}

function SessionLink({ sessionId }: { sessionId: string | null }) {
  if (!sessionId) return null;
  return (
    <a
      className="session-link"
      href={`/index.html?session=${encodeURIComponent(sessionId)}`}
      onClick={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
      title={`Linked session ${sessionId}`}
    >
      <svg className="i i--xs" viewBox="0 0 16 16"><path d="M6.2 9.8 3.8 12.2a2 2 0 0 1-2.8-2.8l2.4-2.4M9.8 6.2l2.4-2.4a2 2 0 0 1 2.8 2.8l-2.4 2.4M5.6 10.4l4.8-4.8"/></svg>
      {shortId(sessionId, 10)}
    </a>
  );
}

function TaskCardBody({ task, selected, saving, overlay }: { task: TaskItem; selected?: boolean; saving?: boolean; overlay?: boolean }) {
  return (
    <article
      className={`task-card${selected ? " is-selected" : ""}${saving ? " is-saving" : ""}${overlay ? " drag-overlay" : ""}`}
      data-status={task.status}
    >
      <div className="task-card__top">
        <span className="task-id" title={task.task_id}>{shortId(task.task_id, 12)}</span>
        {task.status === "failed" || task.status === "cancelled" ? <span className="tag tag--bug">{STATUS_LABELS[task.status]}</span> : null}
        {task.pr_url ? <a className="task-ext" href={task.pr_url} onClick={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()}>PR</a> : null}
        {task.issue_url ? <a className="task-ext" href={task.issue_url} onClick={(event) => event.stopPropagation()} onPointerDown={(event) => event.stopPropagation()}>Issue</a> : null}
      </div>
      <h3 className="task-card__title">{taskTitle(task)}</h3>
      {task.description ? <p className="task-card__desc">{task.description}</p> : null}
      <div className="task-card__meta">
        <span className={sourceClassName(task.source)}>{sourceLabel(task.source)}</span>
        <SessionLink sessionId={task.session_id} />
      </div>
      <div className="task-card__foot">
        <span className="todo-count">
          <svg className="i" viewBox="0 0 16 16"><path d="M3.2 4.5h1M6.2 4.5h6.6M3.2 8h1M6.2 8h6.6M3.2 11.5h1M6.2 11.5h4.2"/></svg>
          {taskCount(task)}
        </span>
      </div>
    </article>
  );
}

function DraggableTaskCard({ task, selected, saving, onSelect }: { task: TaskItem; selected: boolean; saving: boolean; onSelect: () => void }) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({ id: task.task_id });
  const style = transform ? { transform: `translate3d(${transform.x}px, ${transform.y}px, 0)` } : undefined;
  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`task-card-slot${isDragging ? " is-dragging" : ""}`}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect();
        }
      }}
      {...listeners}
      {...attributes}
    >
      <TaskCardBody task={task} selected={selected} saving={saving} />
    </div>
  );
}

function DroppableColumn({ column, tasks, selectedId, savingId, onSelect }: { column: Column; tasks: TaskItem[]; selectedId: string | null; savingId: string | null; onSelect: (id: string) => void }) {
  const { setNodeRef, isOver } = useDroppable({
    id: column.key,
    data: { status: column.statuses[0] },
    disabled: !column.droppable
  });
  return (
    <section ref={setNodeRef} className={`column${isOver ? " is-over" : ""}`} data-status={column.key}>
      <div className="column__head">
        <span className="dot" />
        <span className="column__name">{column.label}</span>
        <span className="column__count">{tasks.length}</span>
      </div>
      <div className="column__body">
        {tasks.map((task) => (
          <DraggableTaskCard
            key={task.task_id}
            task={task}
            selected={task.task_id === selectedId}
            saving={savingId === task.task_id}
            onSelect={() => onSelect(task.task_id)}
          />
        ))}
        {tasks.length === 0 ? <div className="column__empty">None</div> : null}
      </div>
    </section>
  );
}

function TaskDetail({
  task,
  saving,
  onClose,
  onMove
}: {
  task: TaskItem;
  saving: boolean;
  onClose: () => void;
  onMove: (status: string) => void;
}) {
  return (
    <aside className="task-detail" aria-label="Task detail">
      <header className="task-detail__head">
        <div>
          <span className="task-detail__id">{task.task_id}</span>
          <h2>{taskTitle(task)}</h2>
        </div>
        <button className="icon-btn" onClick={onClose} aria-label="Close task detail">×</button>
      </header>
      <div className="task-detail__body">
        <section className="task-detail__section">
          <h3>Description</h3>
          <p>{task.description || "No description."}</p>
        </section>
        <section className="task-detail__section">
          <h3>Status</h3>
          <select value={task.status} disabled={saving} onChange={(event) => onMove(event.target.value)} aria-label="Task status">
            {STATUS_OPTIONS.map((status) => <option key={status} value={status}>{STATUS_LABELS[status]}</option>)}
          </select>
        </section>
        <section className="task-detail__section">
          <h3>Progress</h3>
          {task.progress.length ? <ul>{task.progress.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul> : <p className="muted">No progress notes.</p>}
        </section>
        <section className="task-detail__section">
          <h3>Todos</h3>
          {task.todos.length ? (
            <ul className="task-detail__todos">
              {task.todos.map((todo) => (
                <li key={todo.todo_id} className={todo.done ? "done" : ""}><span className="box">{todo.done ? "✓" : ""}</span>{todo.text}</li>
              ))}
            </ul>
          ) : <p className="muted">No todos.</p>}
        </section>
        <section className="task-detail__section task-detail__meta">
          <h3>Links</h3>
          <div><span className={sourceClassName(task.source)}>{sourceLabel(task.source)}</span></div>
          <SessionLink sessionId={task.session_id} />
          {task.issue_url ? <a href={task.issue_url} target="_blank" rel="noreferrer">Issue</a> : null}
          {task.pr_url ? <a href={task.pr_url} target="_blank" rel="noreferrer">PR</a> : null}
        </section>
      </div>
    </aside>
  );
}

export function TasksPage() {
  const live = useLiveSnapshot();
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string>("");

  useEffect(() => {
    if (savingId === null) setTasks(live.snapshot?.tasks ?? []);
  }, [live.snapshot, savingId]);

  useEffect(() => {
    const requested = new URLSearchParams(location.search).get("task");
    if (requested && tasks.some((task) => task.task_id === requested)) setSelectedId(requested);
  }, [tasks]);

  const visibleTasks = useMemo(() => {
    const text = query.trim().toLowerCase();
    return tasks.filter((task) => {
      const matchesText =
        !text ||
        taskTitle(task).toLowerCase().includes(text) ||
        task.task_id.toLowerCase().includes(text) ||
        task.description.toLowerCase().includes(text);
      const matchesFilter =
        filter === "all" ||
        (filter === "running" && task.status === "running") ||
        (filter === "done" && task.status === "completed");
      return matchesText && matchesFilter;
    });
  }, [tasks, query, filter]);

  const activeCount = tasks.filter((task) => !["completed", "failed", "cancelled"].includes(task.status)).length;
  const selected = tasks.find((task) => task.task_id === selectedId) ?? null;
  const activeTask = tasks.find((task) => task.task_id === activeId) ?? null;

  async function moveTask(task: TaskItem, targetStatus: string) {
    if (targetStatus === task.status || savingId !== null) return;
    const previous = tasks;
    setTasks((current) => current.map((item) => item.task_id === task.task_id ? { ...item, status: targetStatus } : item));
    setSavingId(task.task_id);
    setNotice("Saving move…");
    try {
      const result = await updateTaskStatus(task.task_id, targetStatus, task.revision);
      setTasks((current) => current.map((item) => item.task_id === task.task_id ? result.task : item));
      setNotice("Saved. Agent execution unchanged.");
      window.setTimeout(() => setNotice(""), 1800);
    } catch (error) {
      setTasks(previous);
      if (error instanceof TaskUpdateError && error.code === "revision_conflict") {
        setNotice("Task changed elsewhere. Reloaded current state.");
        await live.reload();
      } else {
        setNotice("Save failed. Reverted the card.");
      }
      window.setTimeout(() => setNotice(""), 2400);
    } finally {
      setSavingId(null);
    }
  }

  function onDragStart(event: DragStartEvent) {
    setActiveId(String(event.active.id));
  }

  function onDragEnd(event: DragEndEvent) {
    setActiveId(null);
    const task = tasks.find((item) => item.task_id === String(event.active.id));
    const column = COLUMNS.find((item) => item.key === String(event.over?.id));
    if (!task || !column || !column.droppable || column.statuses.length !== 1) return;
    void moveTask(task, column.statuses[0]);
  }

  function onDragCancel() {
    setActiveId(null);
    setNotice("Move cancelled. Card returned to its place.");
    window.setTimeout(() => setNotice(""), 1600);
  }

  const filterTasks = (column: Column) => visibleTasks.filter((task) => columnForTask(task) === column.key);

  return (
    <div className="shell">
      <Sidebar active="tasks" sessionsCount={live.snapshot?.sessions.length ?? 0} tasksCount={tasks.length}>
        <button className="sidebar__action" disabled title="Read-only W1">
          <span aria-hidden="true">+</span> New task
        </button>
        <div className="side-filters">
          <button className={`side-filter${filter === "all" ? " is-active" : ""}`} onClick={() => setFilter("all")}>
            All tasks <span className="count">{tasks.length}</span>
          </button>
          <button className={`side-filter${filter === "running" ? " is-active" : ""}`} onClick={() => setFilter("running")}>
            <span className="dot dot--live" /> In progress <span className="count">{tasks.filter((task) => task.status === "running").length}</span>
          </button>
          <button className={`side-filter${filter === "done" ? " is-active" : ""}`} onClick={() => setFilter("done")}>
            Done <span className="count">{tasks.filter((task) => task.status === "completed").length}</span>
          </button>
        </div>
        <div className="project-label">Project</div>
        <div className="project-row">
          <svg className="i i--sm" viewBox="0 0 16 16"><path d="M2.5 4.2h4l1.2 1.4h5.8v7.2a1 1 0 0 1-1 1h-10a1 1 0 0 1-1-1V4.2Z"/></svg>
          {tasks[0]?.project || "cairn"}
        </div>
      </Sidebar>

      <main className="tasks-main">
        <header className="tasks-head">
          <div className="tasks-head__title">
            <h1>Tasks</h1>
            <span className="count-pill">{activeCount} active</span>
          </div>
          <div className="tasks-head__actions">
            <SyncBadge sync={live.sync} />
            <span className="read-only-badge">Read-only</span>
          </div>
        </header>

        <div className="tasks-toolbar">
          <label className="board-search">
            <svg className="i i--xs" viewBox="0 0 16 16"><circle cx="7.2" cy="7.2" r="4.1"/><path d="m10.3 10.3 2.7 2.7"/></svg>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search tasks" aria-label="Search tasks" />
          </label>
        </div>

        {live.error ? <div className="empty-state board-empty">Snapshot unavailable. Check the startup roots and TaskBoard store.</div> : null}
        {!live.error && live.snapshot?.source_status.tasks === "unavailable" ? <div className="empty-state board-empty">TaskBoard unavailable.</div> : null}
        {!live.error && live.snapshot?.source_status.tasks === "missing" ? <div className="empty-state board-empty">No TaskBoard at the configured root.</div> : null}
        {!live.snapshot && !live.error ? <div className="empty-state board-empty">Loading tasks…</div> : null}

        <DndContext sensors={DRAG_SENSORS} collisionDetection={closestCorners} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={onDragCancel}>
          <div className="board-scroll">
            <div className="board" id="board">
              {COLUMNS.map((column) => (
                <DroppableColumn
                  key={column.key}
                  column={column}
                  tasks={filterTasks(column)}
                  selectedId={selectedId}
                  savingId={savingId}
                  onSelect={setSelectedId}
                />
              ))}
            </div>
          </div>
          <DragOverlay dropAnimation={{ duration: 220, easing: "cubic-bezier(.2,.7,.25,1)" }}>
            {activeTask ? <div className="drag-overlay-wrap"><TaskCardBody task={activeTask} overlay /></div> : null}
          </DragOverlay>
        </DndContext>

        {notice ? <div className="task-notice" role="status">{notice}</div> : null}
        {selected ? (
          <TaskDetail task={selected} saving={savingId === selected.task_id} onClose={() => setSelectedId(null)} onMove={(status) => void moveTask(selected, status)} />
        ) : null}
      </main>
    </div>
  );
}
