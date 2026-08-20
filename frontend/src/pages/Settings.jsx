import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function Settings() {
  const [health, setHealth] = useState(null);
  const [agent, setAgent] = useState(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
    api.agentConfig().then(setAgent).catch(() => {});
  }, []);

  const dot = (up) => <span className={`tag ${up ? "ok" : "err"}`}>{up ? "up" : "down"}</span>;

  return (
    <>
      <div className="head"><h2>Settings</h2></div>

      <div className="panel">
        <h3>Services</h3>
        <table>
          <tbody>
            {health && Object.entries(health).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td>{dot(v)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>LLM backend (agent)</h3>
        {agent ? (
          <table>
            <tbody>
              <tr><td>base_url</td><td><code>{agent.base_url}</code></td></tr>
              <tr><td>model</td><td><code>{agent.model}</code></td></tr>
              <tr><td>API key</td><td>{agent.api_key_set ? <span className="tag ok">set</span> : <span className="tag err">missing</span>}</td></tr>
              <tr><td>tools</td><td>{(agent.tools || []).join(", ")}</td></tr>
            </tbody>
          </table>
        ) : <div className="hint">loading…</div>}
        <div className="hint" style={{ marginTop: 10 }}>
          Change these in <code>backend/.env</code> (OLLAMA_MODEL, OLLAMA_API_KEY) and restart the agent service.
        </div>
      </div>
    </>
  );
}
