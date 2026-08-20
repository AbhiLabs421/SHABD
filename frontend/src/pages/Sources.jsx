import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

// Tool Sources — mount a YAML-defined REST API as a live spell (no Python).
export default function Sources() {
  const [sources, setSources] = useState([]);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [method, setMethod] = useState("GET");
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");

  const load = () => api.sources().then((d) => setSources(d.sources || [])).catch(() => {});
  useEffect(() => {
    load();
  }, []);

  const connect = async () => {
    setErr(""); setOk("");
    try {
      // params: parse {city} placeholders from the URL into string params
      const params = {};
      [...url.matchAll(/\{(\w+)\}/g)].forEach((m) => {
        params[m[1]] = { type: "string", required: true };
      });
      await api.connectSource({ name, url, method, params });
      setOk(`Mounted "${name}" — it's now a spell on the Spells page.`);
      setName(""); setUrl("");
      load();
    } catch (e) {
      setErr(e.message);
    }
  };

  return (
    <>
      <div className="head"><h2>Tool Sources</h2></div>
      <div className="panel">
        <div className="hint">
          Attach external REST APIs as spells with no Python — just a name, a URL
          (use <code>{"{placeholders}"}</code> for parameters), and a method.
        </div>
      </div>
      <div className="panel">
        <h3>Add a YAML REST source</h3>
        <div className="row">
          <div>
            <label>Spell name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="get_weather" />
          </div>
          <div style={{ flex: 2 }}>
            <label>URL template</label>
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://wttr.in/{city}?format=j1" />
          </div>
          <div style={{ flex: "0 0 110px" }}>
            <label>Method</label>
            <select value={method} onChange={(e) => setMethod(e.target.value)}>
              <option>GET</option><option>POST</option>
            </select>
          </div>
          <button onClick={connect}>Connect</button>
        </div>
        {err && <div className="error">{err}</div>}
        {ok && <div className="hint" style={{ color: "var(--ok)" }}>{ok}</div>}
      </div>
      <div className="panel">
        <h3>Mounted sources</h3>
        <table>
          <thead><tr><th>name</th><th>type</th><th>url</th></tr></thead>
          <tbody>
            {sources.map((s) => <tr key={s.name}><td><code>{s.name}</code></td><td>{s.type}</td><td>{s.url}</td></tr>)}
            {sources.length === 0 && <tr><td colSpan="3" className="empty">No sources mounted.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
