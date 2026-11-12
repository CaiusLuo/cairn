import { flushSync } from "react-dom";

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const storageKey = "cairn-theme";

/** Read the saved preference, accepting the older paper/slate names. */
export function readThemePreference(): ThemePreference | null {
  try {
    const stored = localStorage.getItem(storageKey);
    if (stored === "paper") return "light";
    if (stored === "slate") return "dark";
    if (stored === "light" || stored === "dark" || stored === "system") return stored;
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

export function resolveTheme(
  preference: ThemePreference | null,
  systemDark = matchMedia("(prefers-color-scheme: dark)").matches,
): ResolvedTheme {
  if (preference === "system") return systemDark ? "dark" : "light";
  return preference ?? timeOfDayTheme();
}

export function applyTheme(theme: ResolvedTheme): void {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

/** Run a theme change as a page crossfade when the browser and user allow motion. */
export function transitionTheme(update: () => void): void {
  const transitionDocument = document as Document & {
    startViewTransition?: (callback: () => void) => unknown;
  };
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches && transitionDocument.startViewTransition) {
    transitionDocument.startViewTransition(() => flushSync(update));
  } else {
    update();
  }
}
