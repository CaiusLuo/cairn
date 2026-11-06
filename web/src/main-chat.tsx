import { createRoot } from "react-dom/client";
import { ChatPage } from "./ChatPage";
import "./styles/shared.css";
import "./styles/runtime.css";
createRoot(document.getElementById("root")!).render(<ChatPage />);
