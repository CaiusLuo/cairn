import { useEffect, useState } from "react";

export type Theme = "slate" | "paper";

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    const requested = new URLSearchParams(location.search).get("theme");
    const stored = localStorage.getItem("cairn-theme");
    return requested === "paper" || (requested === null && stored === "paper") ? "paper" : "slate";
  });

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("cairn-theme", theme);
  }, [theme]);

  return [theme, () => setTheme((value) => (value === "paper" ? "slate" : "paper"))];
}
