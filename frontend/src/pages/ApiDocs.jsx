import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

const SERVICES = [
  ["gateway", 8000],
  ["spells", 8001],
  ["notary", 8002],
  ["users", 8003],
  ["agent", 8004],
];

export default function ApiDocs() {
  const [manifest, setManifest] = useState(null);
  useEffect(() => {
    api.spells().then(setManifest).catch(() => {});
  }, []);
  return (
    <>
      <div className="head"><h2>API Docs</h2></div>
      <div className="panel">
        <h3>Interactive Swagger (per service)</h3>
        <div className="hint" style={{ marginBottom: 10 }}>
          Every microservice is a FastAPI app with built-in Swagger UI at <code>/docs</code>.
        </div>
        <table>
          <thead><tr><th>service</th><th>Swagger</th></tr></thead>
          <tbody>
            {SERVICES.map(([name, port]) => (
              <tr key={name}>
                <td>{name}_service</td>
                <td><a href={`http://127.0.0.1:${port}/docs`} target="_blank" rel="noreferrer">http://127.0.0.1:{port}/docs</a></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h3>Spell manifest (AI-friendly)</h3>
        <pre>{manifest ? JSON.stringify(manifest, null, 2) : "loading…"}</pre>
      </div>
    </>
  );
}
