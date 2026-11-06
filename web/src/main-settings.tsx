import { createRoot } from "react-dom/client";
import { SettingsPage } from "./SettingsPage";
import "./styles/shared.css";
import "./styles/runtime.css";
createRoot(document.getElementById("root")!).render(<SettingsPage />);
