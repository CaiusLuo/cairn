import { createRoot } from "react-dom/client";
import { SessionsPage } from "./sessions/SessionsPage";
import "./styles/shared.css";
import "./styles/session.css";

createRoot(document.getElementById("root")!).render(<SessionsPage />);
