import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { RuntimeError, runtimeApi } from "../runtime-api";
import { Chat } from "./Chat";
import { useResource, type Conversation, type Run, type Task, type WorkbenchConfig } from "./data";
import { Runs, Tasks } from "./execution";
import { href, Shell, type View } from "./Shell";

// Heavy, route-specific views stay out of the initial chat bundle.
const Approvals = lazy(() => import("./Approvals").then(module => ({ default: module.Approvals })));
const Knowledge = lazy(() => import("./Knowledge").then(module => ({ default: module.Knowledge })));
const Channels = lazy(() => import("./Channels").then(module => ({ default: module.Channels })));
const SettingsPage = lazy(() => import("../SettingsPage").then(module => ({ default: module.SettingsPage })));
const lastConversationKey = "cairn.web.last-conversation";

function currentRoute() {
  const params = new URLSearchParams(location.search);
  const requested = params.get("view");
  const alias: Record<string, View> = { "/chat.html": params.has("task") ? "tasks" : "chat", "/settings.html": "settings", "/tasks.html": "tasks", "/knowledge.html": "knowledge", "/channels.html": "channels" };
  // The graph view is gone; old bookmarks still land on their task.
  const view = requested === "workflow" ? "tasks" : requested === "runs" || requested === "approvals" || requested === "knowledge" ? requested : alias[location.pathname] ?? "home";
  return { view, params };
}

export function App() {
  const [route, setRoute] = useState(currentRoute);
  const [navigationEpoch, setNavigationEpoch] = useState(0);
  const [lastConversation, setLastConversation] = useState(() => sessionStorage.getItem(lastConversationKey) ?? "");
  const chatReturn = useRef<AbortController | null>(null);
  const { view, params } = route;
  const config = useResource<WorkbenchConfig>("management?section=workbench", 4500);
  const conversations = useResource<Conversation[]>("conversations", 2000);
  const tasks = useResource<Task[]>(["tasks", "approvals"].includes(view) ? "tasks" : null, 1800);
  const runs = useResource<Run[]>(view === "runs" || view === "approvals" ? "runs" : null, 1800);
  const development = (tasks.data ?? []).filter(item => item.mode === "development" || item.worktree !== null || item.task_id === params.get("task"));
  const id = params.get("conversation") ?? "";
  const task = params.get("task") ?? "";
  const run = params.get("run") ?? "";
  const inChat = view === "home" || view === "chat";
  const chatHref = href("chat", !inChat && lastConversation ? { conversation: lastConversation } : undefined);

  const cancelChatReturn = useCallback(() => {
    chatReturn.current?.abort();
    chatReturn.current = null;
  }, []);

  useEffect(() => {
    const update = () => {
      cancelChatReturn();
      setRoute(currentRoute());
    };
    window.addEventListener("popstate", update);
    return () => {
      window.removeEventListener("popstate", update);
      cancelChatReturn();
    };
  }, [cancelChatReturn]);

  const navigate = useCallback((url: string) => {
    cancelChatReturn();
    if (location.pathname + location.search !== url) window.history.pushState({}, "", url);
    setRoute(currentRoute());
    setNavigationEpoch(value => value + 1);
    document.querySelector(".wb-main")?.scrollTo(0, 0);
  }, [cancelChatReturn]);
  const refresh = useCallback(() => { conversations.refresh(); tasks.refresh(); runs.refresh(); }, [conversations.refresh, tasks.refresh, runs.refresh]);
  const rememberConversation = useCallback((conversationId: string) => {
    sessionStorage.setItem(lastConversationKey, conversationId);
    setLastConversation(conversationId);
  }, []);

  async function openChat() {
    if (inChat && !id) return;
    if (inChat || !lastConversation) {
      navigate(chatHref);
      return;
    }

    cancelChatReturn();
    const request = new AbortController();
    chatReturn.current = request;
    try {
      await runtimeApi(`conversations/${encodeURIComponent(lastConversation)}`, undefined, request.signal);
    } catch (error) {
      if (request.signal.aborted || (error instanceof RuntimeError && error.status === 401)) return;
      if (error instanceof RuntimeError && error.status === 404) {
        sessionStorage.removeItem(lastConversationKey);
        setLastConversation("");
        navigate(href("chat"));
        return;
      }
    }
    if (!request.signal.aborted) navigate(chatHref);
  }

  return <Shell view={view} active={id} navigate={navigate} chatHref={chatHref} onChatClick={openChat} conversations={conversations.data ?? []} workspace={config.data?.workspace}
    status={config.error ? "offline" : config.data ? "online" : "connecting"} onConversationsChanged={conversations.refresh}>
    <Suspense fallback={<p className="wb-muted" role="status">正在加载界面…</p>}>
      {inChat && <Chat key={id || params.get("mode") || "new"} id={id} initialMode={params.get("mode") === "development" ? "development" : "conversation"} config={config.data} navigate={navigate} onSubmit={refresh} onConversationOpened={rememberConversation}/>}
      {view === "tasks" && <Tasks tasks={development} id={task} navigate={navigate} refresh={refresh} loading={!tasks.data} error={tasks.error}/>}
      {view === "knowledge" && <Knowledge/>}
      {view === "channels" && <Channels key={navigationEpoch}/>}
      {view === "runs" && <Runs runs={runs.data ?? []} id={run} navigate={navigate} error={runs.error}/>}
      {view === "approvals" && <Approvals task={task} run={run} tasks={development} runs={runs.data ?? []} navigate={navigate} refresh={refresh} error={runs.error || tasks.error}/>}
      {view === "settings" && <SettingsPage key={params.get("section")} />}
    </Suspense>
  </Shell>;
}
