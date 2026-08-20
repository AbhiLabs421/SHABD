import { useState } from "react";
import { api } from "../lib/api.js";

const SAMPLE = `@app.spell(tags=["demo"])
def greet(name: str) -> str:
    """Say hello."""
    return f"Namaste {name}!"`;

// Builder — paste Python, register it as a live spell (server-side exec).
export default function Builder() {
  const [code, setCode] = useState(SAMPLE);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const build = async () => {
    setErr(""); setMsg(""); setBusy(true);
    try {
      const r = await api.buildSpell(code);
      setMsg(`Registered: ${r.registered.join(", ")} — now on the Spells page.`);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="head"><h2>Builder</h2></div>
      <div className="panel">
        <div className="hint">
          Write a Python function, register it as a spell instantly — no redeploy.
          <b> Note:</b> this runs code server-side; in production gate it to superusers
          and sandbox the exec.
        </div>
      </div>
      <div className="panel">
        <h3>New spell</h3>
        <textarea rows="10" value={code} onChange={(e) => setCode(e.target.value)} />
        <div style={{ marginTop: 12 }}>
          <button disabled={busy} onClick={build}>{busy ? "registering…" : "Register spell"}</button>
        </div>
        {err && <div className="error">{err}</div>}
        {msg && <div className="hint" style={{ color: "var(--ok)" }}>{msg}</div>}
      </div>
    </>
  );
}
