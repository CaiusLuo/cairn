import { Sidebar } from "./components/Sidebar";
import { ConversationPanel } from "./ConversationPanel";

export function ChatPage() {
  return (
    <div className="app-shell">
      <Sidebar active="chat" />
      <main className="chat-main">
        <ConversationPanel />
      </main>
    </div>
  );
}
