import { createRoot } from "react-dom/client";
import { LoginPage } from "./login/LoginPage";
import "./styles/shared.css";
import "./styles/login.css";

createRoot(document.getElementById("root")!).render(<LoginPage />);
