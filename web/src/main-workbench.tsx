import { createRoot } from "react-dom/client";
import "./vendor/harness/tokens.css";
import "./workbench/workbench.css";

const root = createRoot(document.getElementById("root")!);
if (location.pathname === "/login" || location.pathname === "/login.html") {
  const { LoginPage } = await import("./login/LoginPage");
  root.render(<LoginPage />);
} else {
  const { App } = await import("./workbench/App");
  root.render(<App />);
}
