import { Sidebar } from "./components/Sidebar";
import { RuntimePanel } from "./tasks/RuntimePanel";
export function ChatPage() { return <div className="app-shell"><Sidebar active="chat" /><main className="chat-main"><RuntimePanel /></main></div>; }
