import { useEffect, useState, type ReactNode } from "react";
import { CairnMark, Icon, type IconName } from "./Icon";
import { isActive, sourceLabel, type Conversation } from "./data";

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

export function useAppearance() {
  const [theme, setTheme] = useState(() => {
    const prior = localStorage.getItem("cairn-theme");
    return prior === "paper" ? "light" : prior === "slate" ? "dark" : prior ?? "system";
  });
  useEffect(() => {
    const system = matchMedia("(prefers-color-scheme: dark)");
    const update = () => {
      document.documentElement.dataset.theme = theme === "system" ? (system.matches ? "dark" : "light") : theme;
    };
    update();
    system.addEventListener("change", update);
    localStorage.setItem("cairn-theme", theme);
    return () => system.removeEventListener("change", update);
  }, [theme]);
  return { theme, setTheme };
}

export function Shell({ view, active, conversations, workspace, status, navigate, children }: {
  view: View;
  active?: string;
  conversations: Conversation[];
  workspace?: string;
  status: "online" | "offline" | "connecting";
  navigate: (url: string) => void;
  children: ReactNode;
}) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("cairn-sidebar") === "closed");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [filter, setFilter] = useState("");
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [visible, setVisible] = useState(12);
  const { theme, setTheme } = useAppearance();
  const items = conversations.filter(item => item.initial_prompt.toLocaleLowerCase().includes(filter.toLocaleLowerCase()));
  const current = navigation.find(item => item.view === view);
  const connection = { online: "Server 已连接", offline: "Server 已断开", connecting: "正在连接 Server" }[status];

  function link(url: string, label: string, icon: IconName, selected: boolean) {
    return <a href={url} className="rail-link" aria-label={label} aria-current={selected ? "page" : undefined} title={label} onClick={event => { if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); setMobileOpen(false); navigate(url); } }}><Icon name={icon} size={19} /></a>;
  }

  return <div className={`workbench ${collapsed ? "side-collapsed" : ""} ${mobileOpen ? "side-mobile-open" : ""}`}>
    <nav className="wb-rail" aria-label="主导航">
      <div className="rail-group">{navigation.map(item => <div key={item.view}>{link(href(item.view), item.label, item.icon, view === item.view)}</div>)}</div>
      <div className="rail-group">
        <button className="rail-link" aria-label="切换会话侧栏" aria-controls="workbench-sidebar" aria-expanded={mobileOpen || !collapsed} title="切换会话侧栏" onClick={() => { if (matchMedia("(max-width: 800px)").matches) setMobileOpen(value => !value); else { setCollapsed(!collapsed); localStorage.setItem("cairn-sidebar", collapsed ? "open" : "closed"); } }}><Icon name="panel-left" /></button>
        {link(href("settings"), "设置", "settings", view === "settings")}
        <button className="rail-link" aria-label="切换主题" title="切换主题" onClick={() => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark")}><Icon name={theme === "system" ? "monitor" : theme === "dark" ? "moon" : "sun"} /></button>
      </div>
    </nav>
    {mobileOpen && <button className="wb-scrim" aria-label="关闭会话侧栏" onClick={() => setMobileOpen(false)} />}
    <aside id="workbench-sidebar" className="wb-side">
      <div className="wb-side-card">
        <div className="wb-brand"><CairnMark size={24}/><strong>Cairn</strong><button className="icon-button" aria-label="关闭会话侧栏" title="关闭会话侧栏" onClick={() => { setMobileOpen(false); if (!matchMedia("(max-width: 800px)").matches) { setCollapsed(true); localStorage.setItem("cairn-sidebar", "closed"); } }}><Icon name="panel-left" size={15}/></button></div>
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
            <a href={href("chat", { conversation: item.conversation_id })} aria-current={active === item.conversation_id ? "page" : undefined} title={item.initial_prompt} onClick={event => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); navigate(event.currentTarget.getAttribute("href")!); setMobileOpen(false); }}>
              <span>{item.initial_prompt}</span>{item.latest_run && isActive(item.latest_run.status) ? <i className="wb-live-dot" aria-label="执行中"/> : <small>{sourceLabel(item.source)}</small>}
            </a>
          </li>)}</ul> : <p>{filter ? "没有匹配的对话" : "暂无对话"}</p>}
          {items.length > visible && <button className="side-more" onClick={() => setVisible(value => value + 12)}>展开其余 {items.length - visible} 条</button>}
        </details>
        <div className="wb-connection" role="status"><span data-connection={status}/>{connection}</div>
      </div>
    </aside>
    <main className="wb-main" aria-label={current?.label ?? "设置"}>{children}</main>
  </div>;
}
