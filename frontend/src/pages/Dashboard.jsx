import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function Dashboard() {
  const [health, setHealth] = useState(null);
  const [grim, setGrim] = useState(null);
  const [spells, setSpells] = useState([]);

  const load = async () => {
    try {
      setHealth(await api.health());
      setGrim(await api.grimoireVerify());
      setSpells((await api.spells()).spells || []);
    } catch (e) {
      /* ignore transient */
    }
  };
  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const svc = (name) => {
    const up = health?.[name];
    return <span className={`tag ${up ? "ok" : "err"}`}>{up ? "up" : "down"}</span>;
  };

  return (
    <>
      <h2>Dashboard</h2>
      <div className="cards">
        <div className="card">
          <div className="label">Spells</div>
          <div className="value">{spells.length}</div>
        </div>
        <div className="card">
          <div className="label">Audit pages</div>
          <div className="value">{grim?.pages ?? "—"}</div>
        </div>
        <div className="card">
          <div className="label">Chain</div>
          <div className="value">{grim ? (grim.ok ? "✓" : "✗") : "—"}</div>
          <div className="hint">{grim?.ok ? "verified" : grim?.reason || ""}</div>
        </div>
      </div>

      <div className="panel">
        <h3>Services (crash-isolated)</h3>
        <div className="hint" style={{ marginBottom: 10 }}>
          Each is an independent process. If one goes down, only its features stop —
          the rest keep running.
        </div>
        <table>
          <tbody>
            <tr><td>users_service (auth)</td><td>{svc("users")}</td></tr>
            <tr><td>spells_service (engine + grimoire)</td><td>{svc("spells")}</td></tr>
            <tr><td>notary_service (witness)</td><td>{svc("notary")}</td></tr>
            <tr><td>agent_service (Ollama LLM)</td><td>{svc("agent")}</td></tr>
          </tbody>
        </table>
      </div>
    </>
  );
}
