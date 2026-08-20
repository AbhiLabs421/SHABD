import { useState } from "react";
import { api } from "../lib/api.js";

// Orchestrator — classify a query to an intent, then route it to a sub-agent.
export default function Orchestrator() {
  const [query, setQuery] = useState("");
  const [cls, setCls] = useState(null);
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const classify = async () => {
    setErr(""); setRun(null); setBusy("classify");
    try { setCls(await api.orchestratorClassify(query)); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  };
  const runIt = async () => {
    setErr(""); setBusy("run");
    try { setRun(await api.orchestratorRun(query)); }
    catch (e) { setErr(e.message); }
    finally { setBusy(""); }
  };

  return (
    <>
      <div className="head"><h2>Orchestrator</h2></div>
      <div className="panel">
        <div className="hint">
          Classifies a query into an <b>intent</b> (keyword first, then the LLM) and
          routes it to the matching sub-agent. Every classification lands in the Grimoire.
        </div>
      </div>
      <div className="panel">
        <h3>Query</h3>
        <input value={query} onChange={(e) => setQuery(e.target.value)}
               placeholder="e.g. what's the GST on 5000?" />
        <div className="row" style={{ marginTop: 12 }}>
          <button className="ghost" disabled={!!busy} onClick={classify}>
            {busy === "classify" ? "…" : "Classify only"}
          </button>
          <button disabled={!!busy} onClick={runIt}>
            {busy === "run" ? "routing…" : "Classify + run"}
          </button>
        </div>
        {err && <div className="error">{err}</div>}
      </div>
      {cls && !run && (
        <div className="panel">
          <h3>Classification</h3>
          <p>Intent <span className="tag info">{cls.intent}</span> · confidence {cls.confidence} · via {cls.classifier}</p>
          <div className="hint">Registered intents: {cls.intents?.map((i) => i.name).join(", ")}</div>
        </div>
      )}
      {run && (
        <div className="panel">
          <h3>Result</h3>
          <p>Routed to <span className="tag info">{run.intent}</span> (conf {run.confidence}, via {run.classifier}, {run.elapsed_s}s)</p>
          <div className="bubble">{run.answer}</div>
        </div>
      )}
    </>
  );
}
