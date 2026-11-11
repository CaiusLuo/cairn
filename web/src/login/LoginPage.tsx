import { useState } from "react";
import "../styles/login.css";
import { AsciiOcean } from "../vendor/ascii-sharks/react";
import { CairnMark } from "../workbench/Brand";

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
      <AsciiOcean className="login-ocean" sharks={[{ kind: "hammerhead", size: 1 }, { kind: "classic", size: 0.72 }]} />
      <div className="login-vignette" />
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-title">
          <CairnMark size={32}/>
          <h1 id="login-title">Sign in</h1>
        </div>
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
