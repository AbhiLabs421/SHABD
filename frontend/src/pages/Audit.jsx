import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

// Audit Log = the Grimoire chain rendered as a human-readable event log.
export default function Audit() {
  const [pages, setPages] = useState([]);
  const [q, setQ] = useState("");

  const load = () => api.grimoirePages().then((d) => setPages(d.pages || []));
  useEffect(() => {
    load();
  }, []);

  const rows = pages
    .filter((p) => !q || (p.spell || "").includes(q) || (p.subject || "").includes(q))
    .slice()
    .reverse();

  return (
    <>
      <div className="head">
        <h2>Audit Log</h2>
        <button className="ghost" onClick={load}>Refresh</button>
      </div>
      <div className="panel">
        <div className="row">
          <div>
            <label>Filter by spell / subject</label>
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. calculate_gst" />
          </div>
        </div>
        <table>
          <thead>
            <tr><th>seq</th><th>ts</th><th>spell</th><th>subject</th><th>status</th><th>trace</th></tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.seq}>
                <td>{p.seq}</td>
                <td>{p.ts ? new Date(p.ts * 1000).toLocaleTimeString() : "—"}</td>
                <td>{p.spell}</td>
                <td>{p.subject || "—"}</td>
                <td>{p.ok ? <span className="tag ok">ok</span> : <span className="tag err">err</span>}</td>
                <td><code>{(p.trace_id || "").slice(0, 8)}</code></td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan="6" className="empty">No audit events yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
