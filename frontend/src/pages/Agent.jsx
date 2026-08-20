import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function Agent() {
  const [config, setConfig] = useState(null);
  const [prompt, setPrompt] = useState("");
  const [messages, setMessages] = useState([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.agentConfig().then(setConfig).catch(() => {});
  }, []);

  const send = async () => {
    const text = prompt.trim();
    if (!text) return;
    setMessages((m) => [...m, { role: "user", text }]);
    setPrompt("");
    setBusy(true);
    try {
      const res = await api.agentRun(text);
      setMessages((m) => [...m, { role: "assistant", text: res.answer, steps: res.steps }]);
    } catch (e) {
      setMessages((m) => [...m, { role: "assistant", text: "⚠ " + e.message }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h2>Agent</h2>
      <div className="panel">
        <div className="hint">
          {config ? (
            <>Model <b>{config.model}</b> via {config.base_url}{" "}
              {config.api_key_set ? <span className="tag ok">key set</span> : <span className="tag err">no key</span>}
              {" "}· tools: {config.tools?.join(", ")}</>
          ) : "loading…"}
        </div>
      </div>

      <div className="panel" style={{ minHeight: 260 }}>
        {messages.length === 0 && (
          <div className="hint">Ask something like "what is 12.5 times 8?" — the model can call the spells as tools.</div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === "user" ? "msg-user" : ""}>
            <div className="bubble">{m.text}</div>
            {m.steps && m.steps.length > 1 && (
              <div className="hint">({m.steps.length} reasoning steps)</div>
            )}
          </div>
        ))}
        {busy && <div className="hint">thinking…</div>}
      </div>

      <div className="row">
        <div style={{ flex: 1 }}>
          <textarea
            rows="2"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder="Message the agent…"
          />
        </div>
        <button disabled={busy} onClick={send}>Send</button>
      </div>
    </>
  );
}
