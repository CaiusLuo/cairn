import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import "../styles/login.css";
import { AsciiOcean } from "../vendor/ascii-sharks/react";
import { CairnMark } from "../workbench/Brand";
import { Icon, type IconName } from "../workbench/Icon";
import {
  applyTheme,
  readThemePreference,
  resolveTheme,
  storeThemePreference,
  type ThemePreference,
} from "../theme";

let workbenchWarmed = false;

function warmWorkbench() {
  if (workbenchWarmed) return;
  workbenchWarmed = true;
  void import("../workbench/App").catch(() => { workbenchWarmed = false; });
}

const themeOptions: { value: ThemePreference; label: string; icon: IconName }[] = [
  { value: "light", label: "Light theme", icon: "sun" },
  { value: "dark", label: "Dark theme", icon: "moon" },
  { value: "system", label: "Follow system theme", icon: "monitor" },
];

export function LoginPage() {
  const [username, setUsername] = useState("");
  const [passwordValue, setPasswordValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [theme, setTheme] = useState<ThemePreference>(() => readThemePreference() ?? resolveTheme(null));
  const themeRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const system = matchMedia("(prefers-color-scheme: dark)");
    const update = () => applyTheme(resolveTheme(theme, system.matches));
    update();
    if (theme !== "system") return;
    system.addEventListener("change", update);
    return () => system.removeEventListener("change", update);
  }, [theme]);

  useEffect(() => {
    let canceled = false;
    let cleanup: (() => void) | undefined;
    void import("gsap")
      .then(({ gsap }) => {
        const group = themeRef.current;
        if (canceled || !group) return;
        const media = gsap.matchMedia();
        media.add("(prefers-reduced-motion: no-preference)", () => {
          const buttons = Array.from(group.querySelectorAll<HTMLButtonElement>("button"));
          const enter = (event: Event) => {
            gsap.to(event.currentTarget as HTMLElement, {
              y: -2, scale: 1.08, duration: 0.18, ease: "power2.out", overwrite: "auto",
            });
          };
          const leave = (event: Event) => {
            gsap.to(event.currentTarget as HTMLElement, {
              y: 0, scale: 1, duration: 0.24, ease: "power2.out", overwrite: "auto",
            });
          };
          const press = (event: Event) => {
            gsap.fromTo(event.currentTarget as HTMLElement,
              { scale: 0.92 },
              { scale: 1.06, duration: 0.32, ease: "back.out(2)", overwrite: "auto" },
            );
          };
          for (const button of buttons) {
            button.addEventListener("pointerenter", enter);
            button.addEventListener("pointerleave", leave);
            button.addEventListener("pointerdown", press);
          }
          return () => {
            for (const button of buttons) {
              button.removeEventListener("pointerenter", enter);
              button.removeEventListener("pointerleave", leave);
              button.removeEventListener("pointerdown", press);
              gsap.killTweensOf(button);
              gsap.set(button, { clearProps: "transform" });
            }
          };
        });
        cleanup = () => media.revert();
      })
      .catch(() => undefined);
    return () => {
      canceled = true;
      cleanup?.();
    };
  }, []);

  function chooseTheme(next: ThemePreference) {
    const commit = () => {
      applyTheme(resolveTheme(next, matchMedia("(prefers-color-scheme: dark)").matches));
      setTheme(next);
      storeThemePreference(next);
    };
    const transitionDocument = document as Document & {
      startViewTransition?: (update: () => void) => unknown;
    };
    if (!matchMedia("(prefers-reduced-motion: reduce)").matches && transitionDocument.startViewTransition) {
      transitionDocument.startViewTransition(() => flushSync(commit));
    } else {
      commit();
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({ username, auth_passphrase: passwordValue })
      });
      if (!response.ok) {
        setError(response.status === 401 ? "Invalid credentials." : "Sign in unavailable.");
        return;
      }
      // Carry the resolved login theme into the workbench, which shares this key.
      storeThemePreference(theme);
      const next = new URLSearchParams(location.search).get("next");
      const destination = new URL(next ?? "/", location.origin);
      location.assign(destination.origin === location.origin ? destination.href : "/");
    } catch {
      setError("The server is unreachable.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <AsciiOcean className="login-ocean" sharks={[{ kind: "hammerhead", size: 1.8, orbit: [0.16, 0.26] }, { kind: "classic", size: 1.2, orbit: [0.16, 0.26] }]} />
      <div className="login-vignette" />
      <div ref={themeRef} className="login-theme" role="group" aria-label="Theme">
        {themeOptions.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-label={option.label}
            aria-pressed={theme === option.value}
            title={option.label}
            onClick={() => chooseTheme(option.value)}
          >
            <Icon name={option.icon} size={16} />
          </button>
        ))}
      </div>
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-title">
          <CairnMark size={32}/>
          <h1 id="login-title">Sign in</h1>
        </div>
        <form className="login-form" onSubmit={submit}>
          <label>
            <span>Username</span>
            <input value={username} onChange={(event) => setUsername(event.target.value)} onFocus={warmWorkbench} autoComplete="username" autoFocus required />
          </label>
          <label>
            <span>Password</span>
            <input type="password" value={passwordValue} onChange={(event) => setPasswordValue(event.target.value)} autoComplete="current-password" required />
          </label>
          {error ? <p className="login-error" role="alert">{error}</p> : null}
          <button className="login-submit" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
      </section>
    </main>
  );
}
