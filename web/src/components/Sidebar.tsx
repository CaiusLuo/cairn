import type { ReactNode } from "react";
import { useTheme } from "../theme";

type SidebarProps = {
  active: "sessions" | "tasks";
  sessionsCount: number;
  tasksCount: number;
  children?: ReactNode;
};

export function Sidebar({ active, sessionsCount, tasksCount, children }: SidebarProps) {
  const [theme, toggleTheme] = useTheme();
  return (
    <aside className="sidebar" aria-label="Navigation">
      <div className="sidebar__top">
        <a className="brand" href="/index.html" aria-label="Cairn Sessions">
          <svg className="brand__mark" viewBox="0 0 28 30" aria-hidden="true">
            <path d="M6.1 8.2c.2-1.9 2.2-3.4 4.7-3.4 2.3 0 4 1.2 4.2 3 .1 1.6-1.7 2.9-4.1 2.9-2.7 0-4.9-1.1-4.8-2.5Z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
            <path d="M3.9 15.9c.1-2.2 3.2-3.9 6.8-3.9 3.4 0 6.1 1.5 6.2 3.6.1 2-2.8 3.5-6.2 3.5-3.7 0-6.9-1.4-6.8-3.2Z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
            <path d="M7.8 23.4c.1-1.7 2.7-3 5.7-3 3 0 5.4 1.2 5.5 2.8.1 1.7-2.4 2.9-5.5 2.9-3.2 0-5.8-1.1-5.7-2.7Z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
          </svg>
          <span className="brand__name">cairn</span>
        </a>
      </div>
      <nav className="appnav" aria-label="Workspace">
        <a className={`appnav__item${active === "sessions" ? " is-active" : ""}`} href="/index.html">
          <svg className="i" viewBox="0 0 16 16"><path d="M3 3.5h10v8.2a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3.5Z"/><path d="M5.2 6.2h5.6M5.2 8.6h4.2"/></svg>
          Sessions
          <span className="count">{sessionsCount}</span>
        </a>
        <a className={`appnav__item${active === "tasks" ? " is-active" : ""}`} href="/tasks.html">
          <svg className="i" viewBox="0 0 16 16"><path d="M3.2 4.5h1M6.2 4.5h6.6M3.2 8h1M6.2 8h6.6M3.2 11.5h1M6.2 11.5h4.2"/><path d="m2.7 4.4.5.5.8-.9M2.7 7.9l.5.5.8-.9"/></svg>
          Tasks
          <span className="count">{tasksCount}</span>
        </a>
      </nav>
      {children}
      <div className="sidebar__foot">
        <span className="avatar" title="Operator" aria-label="Operator">CL</span>
        <button className="icon-btn" onClick={toggleTheme} title={`Switch to ${theme === "paper" ? "slate" : "paper"} theme`} aria-label="Toggle theme">
          <svg className="i i--sm" viewBox="0 0 16 16"><circle cx="8" cy="8" r="3.1"/><path d="M8 1.1v1.5M8 13.4v1.5M1.1 8h1.5M13.4 8h1.5M3.2 3.2l1.1 1.1M11.7 11.7l1.1 1.1M12.8 3.2l-1.1 1.1M4.3 11.7l-1.1 1.1"/></svg>
        </button>
      </div>
    </aside>
  );
}
