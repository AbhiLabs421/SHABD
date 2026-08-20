import { useState } from "react";
import { api } from "../lib/api.js";

// Client Console — connect to another SHABD server via the gateway's outbound
// proxy, browse its manifest, invoke its spells.
export default function ClientConsole() {
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [manifest, setManifest] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const fetchManifest = async () => {
    setErr(""); setManifest(null); setBusy(true);
    try {
      const m = await api.clientCall({ base_url: url, token, action: "manifest" });
      setManifest(m);
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="head"><h2>Client Console</h2></div>
      <div className="panel">
        <div className="hint">
          Point at another SHABD/MCP server, browse its manifest, and invoke its
          spells from here — the gateway proxies the call so the remote token stays
          server-side and CORS never blocks you.
        </div>
      </div>
      <div className="panel">
        <h3>Connect</h3>
        <div className="row">
          <div>
            <label>Base URL</label>
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://127.0.0.1:8001" />
          </div>
          <div>
            <label>Bearer token (optional)</label>
            <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="ey…" />
          </div>
          <button disabled={busy} onClick={fetchManifest}>{busy ? "…" : "Fetch manifest"}</button>
        </div>
        <div className="hint">Point at any SHABD server started with <code>app.serve()</code> (it exposes <code>/manifest</code>).</div>
        {err && <div className="error">{err}</div>}
      </div>
      {manifest && (
        <div className="panel">
          <h3>Remote manifest</h3>
          <pre>{JSON.stringify(manifest, null, 2)}</pre>
        </div>
      )}
    </>
  );
}
