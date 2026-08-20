import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function Grimoire() {
  const [verify, setVerify] = useState(null);
  const [pages, setPages] = useState([]);

  const load = async () => {
    setVerify(await api.grimoireVerify());
    setPages((await api.grimoirePages()).pages || []);
  };
  useEffect(() => {
    load();
  }, []);

  return (
    <>
      <h2>Grimoire — tamper-evident audit chain</h2>
      <div className="panel">
        <div className="hint">
          Every spell cast becomes a hash-chained, HMAC-signed page. Edit any past
          page and the whole chain breaks. "Tamper detected" here almost always means
          the signing <b>secret changed between restarts</b> — not real tampering.
          A stable <code>shared/.shabd-secret</code> keeps it green.
        </div>
        <div style={{ marginTop: 12 }}>
          {verify?.ok ? (
            <span className="tag ok">Verified — {verify.pages} pages</span>
          ) : (
            <span className="tag err">Tamper: {verify?.reason} @ seq {verify?.at_seq}</span>
          )}
          {verify?.head && (
            <span className="hint" style={{ marginLeft: 12 }}>head {verify.head.slice(0, 24)}…</span>
          )}
          <button className="ghost" style={{ marginLeft: 12 }} onClick={load}>Re-verify</button>
        </div>
      </div>

      <div className="panel">
        <h3>Pages</h3>
        <table>
          <thead>
            <tr><th>seq</th><th>spell</th><th>ok</th><th>hash</th><th>prev</th></tr>
          </thead>
          <tbody>
            {pages.map((p) => (
              <tr key={p.seq}>
                <td>{p.seq}</td>
                <td>{p.spell}</td>
                <td>{p.ok ? "✓" : "✗"}</td>
                <td><code>{p.hash?.slice(0, 12)}…</code></td>
                <td><code>{p.prev?.slice(0, 12)}…</code></td>
              </tr>
            ))}
            {pages.length === 0 && (
              <tr><td colSpan="5" className="hint">No pages yet — cast a spell first.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
