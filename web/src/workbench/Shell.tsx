import { useEffect, useState, type ReactNode } from "react";
import { runtimeApi } from "../runtime-api";
import {
  applyTheme,
  readThemePreference,
  resolveTheme,
  storeThemePreference,
  transitionTheme,
  type ThemePreference,
} from "../theme";
import { CairnLogo } from "./Brand";
import { Icon, type IconName } from "./Icon";
import { conversationTitle, isActive, sourceLabel, type Conversation } from "./data";

export type View = "home" | "chat" | "tasks" | "workflow" | "runs" | "approvals" | "settings";
const navigation: { view: View; label: string; icon: IconName }[] = [
  { view: "home", label: "工作台", icon: "home-start" },
  { view: "chat", label: "对话", icon: "chat" },
  { view: "tasks", label: "开发任务", icon: "board" },
  { view: "approvals", label: "待我确认", icon: "gate" },
  { view: "workflow", label: "流程", icon: "workflow" },
  { view: "runs", label: "运行", icon: "runs" },
];

export function href(view: View, params: Record<string, string> = {}) {
  const query = new URLSearchParams(params);
  if (view === "workflow" || view === "runs" || view === "approvals") query.set("view", view);
  const page = { chat: "/chat.html", tasks: "/tasks.html", settings: "/settings.html" }[view as "chat" | "tasks" | "settings"] ?? "/";
  return page + (query.size ? "?" + query : "");
}

const themeOrder: ThemePreference[] = ["light", "dark", "system"];
const themeLabels: Record<ThemePreference, string> = { light: "亮色", dark: "暗色", system: "跟随系统" };

function mutationError(error: unknown, fallback: string) {
  if (!(error instanceof Error)) return fallback;
  if (error.message === "conversation_busy") return "对话仍在执行，请等待运行结束后再试。";
  return error.message;
}

export function useAppearance() {
  const [theme, setTheme] = useState<ThemePreference>(() => readThemePreference() ?? resolveTheme(null));
  useEffect(() => {
    const system = matchMedia("(prefers-color-scheme: dark)");
    const update = () => applyTheme(resolveTheme(theme, system.matches));
    update();
    if (theme !== "system") return;
    system.addEventListener("change", update);
    return () => system.removeEventListener("change", update);
  }, [theme]);
  function chooseTheme(next: ThemePreference) {
    transitionTheme(() => {
      applyTheme(resolveTheme(next));
      setTheme(next);
      storeThemePreference(next);
    });
  }
  return { theme, chooseTheme };
}

export function Shell({ view, active, conversations, workspace, status, navigate, onConversationsChanged, children }: {
  view: View;
  active?: string;
  conversations: Conversation[];
  workspace?: string;
  status: "online" | "offline" | "connecting";
  navigate: (url: string) => void;
  onConversationsChanged?: () => void;
  children: ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("cairn-sidebar") === "closed");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [narrow, setNarrow] = useState(() => matchMedia("(max-width: 800px)").matches);
  useEffect(() => {
    const media = matchMedia("(max-width: 800px)");
    const update = () => { setNarrow(media.matches); setMobileOpen(false); };
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (!mobileOpen) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setMobileOpen(false); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [mobileOpen]);
  const [searching, setSearching] = useState(false);
  const [filter, setFilter] = useState("");
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [visible, setVisible] = useState(12);
  const [menu, setMenu] = useState<{ id: string; top: number; left: number; up: boolean } | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [mutating, setMutating] = useState(false);
  const { theme, chooseTheme } = useAppearance();
  const items = conversations.filter(item => (item.title || item.initial_prompt).toLocaleLowerCase().includes(filter.toLocaleLowerCase()));
  const current = navigation.find(item => item.view === view);
  const connection = { online: "Server 已连接", offline: "Server 已断开", connecting: "正在连接 Server" }[status];

  useEffect(() => {
    if (!menu) return;
    const close = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (!target?.closest(".wb-session-actions, .wb-session-menu")) setMenu(null);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenu(null); };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", escape);
    return () => { window.removeEventListener("pointerdown", close); window.removeEventListener("keydown", escape); };
  }, [menu]);

  function toggleMenu(item: Conversation, button: HTMLButtonElement) {
    if (menu?.id === item.conversation_id) { setMenu(null); return; }
    const rect = button.getBoundingClientRect();
    const up = rect.bottom + 104 > window.innerHeight;
    setMenu({ id: item.conversation_id, top: up ? rect.top - 5 : rect.bottom + 5, left: rect.right, up });
  }

  function startRename(item: Conversation) {
    setRenaming(item.conversation_id);
    setRenameDraft(item.title || item.initial_prompt);
    setMenu(null);
  }

  async function saveRename(item: Conversation) {
    const title = renameDraft.trim();
    if (!title) { setRenaming(null); return; }
    setMutating(true);
    try {
      await runtimeApi(`conversations/${encodeURIComponent(item.conversation_id)}/rename`, { title });
      setRenaming(null);
      onConversationsChanged?.();
    } catch (error) {
      window.alert(mutationError(error, "重命名失败"));
    } finally {
      setMutating(false);
    }
  }

  async function removeConversation(item: Conversation) {
    setMenu(null);
    const label = conversationTitle(item.title || item.initial_prompt, 40);
    if (!window.confirm(`删除对话「${label}」？该对话的运行记录会一并删除，且无法恢复。`)) return;
    setMutating(true);
    try {
      await runtimeApi(`conversations/${encodeURIComponent(item.conversation_id)}/delete`, {});
      if (active === item.conversation_id) navigate(href("home"));
      onConversationsChanged?.();
    } catch (error) {
      window.alert(mutationError(error, "删除失败"));
    } finally {
      setMutating(false);
    }
  }

  function link(url: string, label: string, icon: IconName, selected: boolean) {
    return <a href={url} className="rail-link" aria-label={label} aria-current={selected ? "page" : undefined} title={label} onClick={event => { if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); setMobileOpen(false); navigate(url); } }}><Icon name={icon} size={19} /></a>;
  }

  return <div className={`workbench ${collapsed ? "side-collapsed" : ""} ${mobileOpen ? "side-mobile-open" : ""}`}>
    <nav className="wb-rail" aria-label="主导航">
      <div className="rail-group">{navigation.map(item => <div key={item.view}>{link(href(item.view), item.label, item.icon, view === item.view)}</div>)}</div>
      <div className="rail-group">
        <button className="rail-link" aria-label="切换会话侧栏" aria-controls="workbench-sidebar" aria-expanded={narrow ? mobileOpen : !collapsed} title="切换会话侧栏" onClick={() => { if (narrow) setMobileOpen(value => !value); else { setCollapsed(!collapsed); localStorage.setItem("cairn-sidebar", collapsed ? "open" : "closed"); } }}><Icon name="panel-left" /></button>
        {link(href("settings"), "设置", "settings", view === "settings")}
        <button className="rail-link" aria-label="切换主题" title={`当前主题：${themeLabels[theme]}（点击切换）`} onClick={() => chooseTheme(themeOrder[(themeOrder.indexOf(theme) + 1) % themeOrder.length])}><Icon name={theme === "system" ? "monitor" : theme === "dark" ? "moon" : "sun"} /></button>
      </div>
    </nav>
    {mobileOpen && <button className="wb-scrim" aria-label="关闭会话侧栏" onClick={() => setMobileOpen(false)} />}
    <aside id="workbench-sidebar" className="wb-side" inert={narrow ? !mobileOpen : collapsed}>
      <div className="wb-side-card">
        <div className="wb-brand"><CairnLogo className="wb-brand-logo"/><button className="icon-button" aria-label="关闭会话侧栏" title="关闭会话侧栏" onClick={() => { setMobileOpen(false); if (!narrow) { setCollapsed(true); localStorage.setItem("cairn-sidebar", "closed"); } }}><Icon name="panel-left" size={15}/></button></div>
        <div className="workspace-switch">
          <button className="workspace-trigger" aria-expanded={workspaceOpen} onClick={() => setWorkspaceOpen(value => !value)}><Icon name="folder" size={15}/><span>{workspace?.split(/[\\/]/).filter(Boolean).at(-1) ?? "工作区"}</span><Icon name="chevron-down" size={13}/></button>
          {workspaceOpen && <div className="workspace-popover"><strong>当前工作区</strong><code>{workspace ?? "等待 Server 连接"}</code><small>由宿主配置</small></div>}
        </div>
        <div className="wb-side-actions">
          <button className="side-new" onClick={() => { navigate(href("home")); setMobileOpen(false); }}><Icon name="edit" size={15}/>新对话</button>
          <button className="icon-button" aria-label="搜索对话" title="搜索对话" aria-expanded={searching} onClick={() => { setSearching(!searching); setFilter(""); }}><Icon name="search" size={16}/></button>
        </div>
        {searching && <input className="side-search" autoFocus placeholder="搜索对话" aria-label="筛选对话" value={filter} onChange={event => setFilter(event.target.value)} onKeyDown={event => { if (event.key === "Escape") { setFilter(""); setSearching(false); } }} />}
        <details className="conversation-tree" open>
          <summary><Icon name="chat" size={15}/>最近对话 <span>{items.length}</span></summary>
          {items.length ? <ul>{items.slice(0, visible).map(item => <li key={item.conversation_id}>
            {renaming === item.conversation_id ? <form className="wb-session-rename" onSubmit={event => { event.preventDefault(); void saveRename(item); }}>
              <input autoFocus aria-label="重命名对话" maxLength={200} value={renameDraft} disabled={mutating} onChange={event => setRenameDraft(event.target.value)} onKeyDown={event => { if (event.key === "Escape") setRenaming(null); }} />
              <button type="submit" aria-label="保存名称" title="保存名称" disabled={mutating || !renameDraft.trim()}><Icon name="check" size={14}/></button>
              <button type="button" aria-label="取消重命名" title="取消重命名" disabled={mutating} onClick={() => setRenaming(null)}><Icon name="close" size={14}/></button>
            </form> : <>
              <a href={href("chat", { conversation: item.conversation_id })} aria-current={active === item.conversation_id ? "page" : undefined} title={item.title || item.initial_prompt} onClick={event => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate(event.currentTarget.getAttribute("href")!); setMobileOpen(false); }}>
                <span>{conversationTitle(item.title || item.initial_prompt)}</span>{item.latest_run && isActive(item.latest_run.status) ? <i className="wb-live-dot" aria-label="执行中"/> : <small>{sourceLabel(item.source)}</small>}
              </a>
              <div className="wb-session-actions">
                <button type="button" className="wb-session-more" aria-label="对话详情" title="对话详情" aria-haspopup="menu" aria-expanded={menu?.id === item.conversation_id} disabled={mutating} onClick={event => { event.preventDefault(); event.stopPropagation(); toggleMenu(item, event.currentTarget); }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="5" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="12" cy="19" r="1.8"/></svg>
                </button>
              </div>
              {menu?.id === item.conversation_id && <div className="wb-session-menu" role="menu" style={{ top: menu.top, left: menu.left, transform: menu.up ? "translate(-100%, -100%)" : "translateX(-100%)" }}>
                <button type="button" role="menuitem" onClick={() => startRename(item)}><Icon name="edit" size={14}/>重命名</button>
                <button type="button" role="menuitem" className="danger" onClick={() => void removeConversation(item)}><Icon name="trash" size={14}/>删除对话</button>
              </div>}
            </>}
          </li>)}</ul> : <p>{filter ? "没有匹配的对话" : "暂无对话"}</p>}
          {items.length > visible && <button className="side-more" onClick={() => setVisible(value => value + 12)}>展开其余 {items.length - visible} 条</button>}
        </details>
        <div className="wb-connection" role="status"><span data-connection={status}/>{connection}</div>
      </div>
    </aside>
    <main className="wb-main" inert={narrow && mobileOpen} aria-label={current?.label ?? "设置"}>{children}</main>
  </div>;
}
