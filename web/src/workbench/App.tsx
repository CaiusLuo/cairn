import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Chat } from "./Chat";
import { useResource, type Conversation, type Run, type Task, type WorkbenchConfig } from "./data";
import { Runs, Tasks } from "./execution";
import { Shell, type View } from "./Shell";

// Heavy, route-specific views stay out of the initial chat bundle. The
// workflow canvas alone pulls in the React Flow renderer.
const Approvals = lazy(() => import("./Approvals").then(module => ({ default: module.Approvals })));
const SettingsPage = lazy(() => import("../SettingsPage").then(module => ({ default: module.SettingsPage })));
const Workflow = lazy(() => import("./Workflow").then(module => ({ default: module.Workflow })));

function currentRoute() {
  const params = new URLSearchParams(location.search);
  const requested = params.get("view");
  const alias: Record<string, View> = { "/chat.html": params.has("task") ? "tasks" : "chat", "/settings.html": "settings", "/tasks.html": "tasks" };
  const view = requested === "workflow" || requested === "runs" || requested === "approvals" ? requested : alias[location.pathname] ?? "home";
  return { view, params };
}

export function App() {
  const [route, setRoute] = useState(currentRoute);
  const { view, params } = route;
  const config = useResource<WorkbenchConfig>("management?section=workbench", 4500);
  const conversations = useResource<Conversation[]>("conversations", 2000);
  const tasks = useResource<Task[]>(["tasks", "workflow", "approvals"].includes(view) ? "tasks" : null, 1800);
  const runs = useResource<Run[]>(view === "runs" || view === "approvals" ? "runs" : null, 1800);
  const development = (tasks.data ?? []).filter(item => item.mode === "development" || item.worktree !== null || item.task_id === params.get("task"));
  const id = params.get("conversation") ?? "";
  const task = params.get("task") ?? "";
  const run = params.get("run") ?? "";

  useEffect(() => {
    const update = () => setRoute(currentRoute());
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  const navigate = useCallback((url: string) => {
    if (location.pathname + location.search !== url) window.history.pushState({}, "", url);
    setRoute(currentRoute());
    document.querySelector(".wb-main")?.scrollTo(0, 0);
  }, []);
  const refresh = useCallback(() => { conversations.refresh(); tasks.refresh(); runs.refresh(); }, [conversations.refresh, tasks.refresh, runs.refresh]);

  return <Shell view={view} active={id} navigate={navigate} conversations={conversations.data ?? []} workspace={config.data?.workspace}
    status={config.error ? "offline" : config.data ? "online" : "connecting"}>
    <Suspense fallback={<p className="wb-muted" role="status">正在加载界面…</p>}>
      {(view === "home" || view === "chat") && <Chat key={id || params.get("mode") || "new"} id={id} initialMode={params.get("mode") === "development" ? "development" : "conversation"} config={config.data} navigate={navigate} onSubmit={refresh}/>}
      {view === "tasks" && <Tasks tasks={development} id={task} navigate={navigate} refresh={refresh} loading={!tasks.data} error={tasks.error}/>}
      {view === "runs" && <Runs runs={runs.data ?? []} id={run} navigate={navigate} error={runs.error}/>}
      {view === "workflow" && <Workflow tasks={development} selected={task} navigate={navigate} error={tasks.error}/>}
      {view === "approvals" && <Approvals task={task} run={run} tasks={development} runs={runs.data ?? []} navigate={navigate} refresh={refresh} error={runs.error || tasks.error}/>}
      {view === "settings" && <SettingsPage key={params.get("section")} />}
    </Suspense>
  </Shell>;
}
