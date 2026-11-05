import { createRoot } from "react-dom/client";
import { TasksPage } from "./tasks/TasksPage";
import "./styles/shared.css";
import "./styles/tasks.css";

createRoot(document.getElementById("root")!).render(<TasksPage />);
