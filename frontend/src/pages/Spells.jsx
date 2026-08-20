import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

function SpellCard({ spell }) {
  const props = spell.input_schema?.properties || spell.parameters?.properties || {};
  const [args, setArgs] = useState({});
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    setError("");
    setResult(null);
    try {
      // coerce numbers where the schema says so
      const payload = {};
      for (const [k, v] of Object.entries(args)) {
        const type = props[k]?.type;
        payload[k] = type === "number" || type === "integer" ? Number(v) : v;
      }
      const res = await api.invoke(spell.name, payload);
      setResult(res.result);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h3>
        {spell.name}{" "}
        {(spell.tags || []).map((t) => (
          <span key={t} className="tag muted" style={{ marginLeft: 6 }}>{t}</span>
        ))}
      </h3>
      <div className="hint">{spell.description}</div>
      <div className="row" style={{ marginTop: 8 }}>
        {Object.entries(props).map(([name, def]) => (
          <div key={name} style={{ flex: "1 1 140px" }}>
            <label>{name} <span style={{ color: "var(--muted)" }}>({def.type})</span></label>
            <input
              value={args[name] ?? ""}
              onChange={(e) => setArgs({ ...args, [name]: e.target.value })}
            />
          </div>
        ))}
      </div>
      <div style={{ marginTop: 12 }}>
        <button disabled={busy} onClick={run}>{busy ? "casting…" : "Cast spell"}</button>
      </div>
      {error && <div className="error">{error}</div>}
      {result !== null && <pre>{JSON.stringify(result, null, 2)}</pre>}
    </div>
  );
}

export default function Spells() {
  const [spells, setSpells] = useState([]);
  useEffect(() => {
    api.spells().then((d) => setSpells(d.spells || []));
  }, []);
  return (
    <>
      <h2>Spells</h2>
      <p className="hint">Every registered tool. Fill the inputs and cast — each call is audited in the Grimoire.</p>
      {spells.map((s) => <SpellCard key={s.name} spell={s} />)}
    </>
  );
}
