import { useState } from "react";
import { AsciiOcean } from "../vendor/ascii-sharks/react";

export function LoginPage() {
  const [username, setUsername] = useState("");
  const [passwordValue, setPasswordValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

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
      const next = new URLSearchParams(location.search).get("next");
      location.assign(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
    } catch {
      setError("The server is unreachable.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <AsciiOcean className="login-ocean" sharks={[{ kind: "hammerhead", size: 1 }, { kind: "classic", size: 0.72 }]} />
      <div className="login-vignette" />
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand">
          <svg className="brand__mark" viewBox="0 0 28 30" aria-hidden="true">
            <path d="M6.1 8.2c.2-1.9 2.2-3.4 4.7-3.4 2.3 0 4 1.2 4.2 3 .1 1.6-1.7 2.9-4.1 2.9-2.7 0-4.9-1.1-4.8-2.5Z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
            <path d="M3.9 15.9c.1-2.2 3.2-3.9 6.8-3.9 3.4 0 6.1 1.5 6.2 3.6.1 2-2.8 3.5-6.2 3.5-3.7 0-6.9-1.4-6.8-3.2Z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
            <path d="M7.8 23.4c.1-1.7 2.7-3 5.7-3 3 0 5.4 1.2 5.5 2.8.1 1.7-2.4 2.9-5.5 2.9-3.2 0-5.8-1.1-5.7-2.7Z" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round"/>
          </svg>
          <span className="brand__name">cairn</span>
        </div>
        <h1 id="login-title">Sign in</h1>
        <form className="login-form" onSubmit={submit}>
          <label>
            <span>Username</span>
            <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" autoFocus required />
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
