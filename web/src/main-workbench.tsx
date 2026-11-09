import { createRoot } from "react-dom/client";
import { Preview } from "./workbench/Preview";
import "./vendor/harness/tokens.css";
import "./workbench/workbench.css";

createRoot(document.getElementById("root")!).render(<Preview />);
