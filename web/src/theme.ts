import { flushSync } from "react-dom";

export type ThemePreference = "light" | "dark";
export type ResolvedTheme = "light" | "dark";
export type ThemeOrigin = { x: number; y: number };

const storageKey = "cairn-theme";

/** Read the saved preference, accepting older names and the removed system mode. */
export function readThemePreference(): ThemePreference | null {
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored === "paper") return "light";
    if (stored === "slate") return "dark";
    if (stored === "light" || stored === "dark") return stored;
    if (stored === "system") {
      return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
  } catch {
    // Storage can be unavailable in private or restricted contexts.
  }
  return null;
}

export function storeThemePreference(preference: ThemePreference): void {
  try {
    localStorage.setItem(storageKey, preference);
  } catch {
    // The theme still applies for this page even if it cannot be remembered.
  }
}

/** First visits follow local time: light from 06:00 to 17:59, dark otherwise. */
export function timeOfDayTheme(now = new Date()): ResolvedTheme {
  const hour = now.getHours();
  return hour >= 6 && hour < 18 ? "light" : "dark";
}

export function resolveTheme(preference: ThemePreference | null): ResolvedTheme {
  return preference ?? timeOfDayTheme();
}

export function applyTheme(theme: ResolvedTheme): void {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

/** Start the reveal at the pointer, or at the control's centre for keyboard use. */
export function themeOrigin(event: {
  clientX: number;
  clientY: number;
  detail: number;
  currentTarget: Element;
}): ThemeOrigin {
  const rect = event.currentTarget.getBoundingClientRect();
  return event.detail > 0
    ? { x: event.clientX, y: event.clientY }
    : { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
}

/** Run a theme change as a circular reveal from the toggle when motion is allowed. */
export function transitionTheme(update: () => void, origin?: ThemeOrigin): void {
  const transitionDocument = document as Document & {
    startViewTransition?: (callback: () => void) => unknown;
  };
  if (!origin || !transitionDocument.startViewTransition || matchMedia("(prefers-reduced-motion: reduce)").matches) {
    update();
    return;
  }
  const radius = Math.hypot(Math.max(origin.x, innerWidth - origin.x), Math.max(origin.y, innerHeight - origin.y));
  const root = document.documentElement;
  root.style.setProperty("--theme-x", `${origin.x}px`);
  root.style.setProperty("--theme-y", `${origin.y}px`);
  root.style.setProperty("--theme-radius", `${radius}px`);
  transitionDocument.startViewTransition(() => flushSync(update));
}
