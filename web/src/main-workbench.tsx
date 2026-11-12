import { createRoot } from "react-dom/client";
import { applyTheme, readThemePreference, resolveTheme } from "./theme";
import { BootScreen } from "./workbench/BootScreen";
import "./vendor/harness/tokens.css";
import "./workbench/workbench.css";

const root = createRoot(document.getElementById("root")!);
const login = location.pathname === "/login" || location.pathname === "/login.html";
const loginTheme = login ? resolveTheme(readThemePreference()) : null;
if (loginTheme) applyTheme(loginTheme);
root.render(
  <BootScreen
    dark={loginTheme === "dark"}
    surface={login ? "login" : undefined}
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
