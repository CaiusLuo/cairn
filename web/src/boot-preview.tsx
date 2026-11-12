import { createRoot } from "react-dom/client";
import { BootScreen } from "./workbench/BootScreen";
import "./vendor/harness/tokens.css";

createRoot(document.getElementById("root")!).render(
  <BootScreen dark label="Loading Cairn…" />,
);
