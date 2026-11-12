import { createRoot } from "react-dom/client";
import { BootScreen } from "./workbench/BootScreen";
import "./vendor/harness/tokens.css";
import "./workbench/workbench.css";

const root = createRoot(document.getElementById("root")!);
const login = location.pathname === "/login" || location.pathname === "/login.html";
root.render(
  <BootScreen
    dark={login}
    label={login ? "Loading Cairn…" : "正在加载 Cairn…"}
  />,
);
if (login) {
  const { LoginPage } = await import("./login/LoginPage");
  root.render(<LoginPage />);
} else {
  const { App } = await import("./workbench/App");
  root.render(<App />);
}
