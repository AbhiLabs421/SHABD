import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

// Spell Chains — pipe spells together (a | b | c). Live-wired to spells_service.
export default function Chains() {
  const [spells, setSpells] = useState([]);
  const [chains, setChains] = useState([]);
  const [name, setName] = useState("");
  const [expr, setExpr] = useState("");
  const [err, setErr] = useState("");

  const load = () => {
    api.spells().then((d) => setSpells(d.spells || []));
    api.chains().then((d) => setChains(d.chains || [])).catch(() => {});
  };
  useEffect(() => {
    load();
  }, []);

  const create = async () => {
    setErr("");
    try {
      await api.createChain(name, expr);
      setName("");
      setExpr("");
      load();
    } catch (e) {
      setErr(e.message);
    }
  };

  return (
    <>
      <div className="head"><h2>Spell Chains</h2></div>
      <div className="panel">
        <div className="hint">
          Connect tools into a pipeline with the pipe operator — the output of one
          step feeds the next: <code>add | multiply</code>. The chain becomes a new
          spell you can cast on the Spells page.
        </div>
      </div>
      <div className="panel">
        <h3>Create a chain</h3>
        <div className="row">
          <div>
            <label>Chain name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="my_pipeline" />
          </div>
          <div style={{ flex: 2 }}>
            <label>Pipeline</label>
            <input value={expr} onChange={(e) => setExpr(e.target.value)} placeholder="add | multiply" />
          </div>
          <button onClick={create}>Create</button>
        </div>
        {err && <div className="error">{err}</div>}
        <div className="hint" style={{ marginTop: 6 }}>Click to append a spell:</div>
        <div style={{ marginTop: 6 }}>
          {spells.map((s) => (
            <span key={s.name} className="tag muted" style={{ marginRight: 6, cursor: "pointer" }}
                  onClick={() => setExpr(expr ? `${expr} | ${s.name}` : s.name)}>{s.name}</span>
          ))}
        </div>
      </div>
      <div className="panel">
        <h3>Existing chains</h3>
        <table>
          <thead><tr><th>name</th><th>pipeline</th></tr></thead>
          <tbody>
            {chains.map((c) => <tr key={c.name}><td><code>{c.name}</code></td><td>{c.pipeline}</td></tr>)}
            {chains.length === 0 && <tr><td colSpan="2" className="empty">No chains yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
