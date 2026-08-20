import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, setSession, getToken } from "../lib/api.js";

export default function Login() {
  const nav = useNavigate();
  const [mode, setMode] = useState("login"); // login | register
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (getToken()) {
    nav("/");
    return null;
  }

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (mode === "register") {
        await api.register(username, password);
      }
      const res = await api.login(username, password);
      setSession(res.token, res.user);
      nav("/");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="panel login-card" onSubmit={submit}>
        <div className="brand" style={{ padding: "0 0 10px" }}>
          🔮 SHABD<small>{mode === "register" ? "create your account" : "sign in to continue"}</small>
        </div>
        <label>Username</label>
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
        <label>Password</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="min 8 characters"
        />
        {error && <div className="error">{error}</div>}
        <div style={{ marginTop: 16 }}>
          <button disabled={busy} type="submit">
            {busy ? "…" : mode === "register" ? "Register & sign in" : "Sign in"}
          </button>
        </div>
        <p className="hint" style={{ marginTop: 14 }}>
          {mode === "register" ? (
            <>Already have an account?{" "}
              <a onClick={() => setMode("login")} href="#">Sign in</a></>
          ) : (
            <>First time here?{" "}
              <a onClick={() => setMode("register")} href="#">Register</a>{" "}
              — the first account becomes the superuser.</>
          )}
        </p>
      </form>
    </div>
  );
}
