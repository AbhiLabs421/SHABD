import { useState } from "react";
import { getToken } from "../lib/api.js";

// Decode a SHABD token (body.sig) — the body is base64url JSON.
function decode(token) {
  try {
    const body = token.split(".")[0];
    const json = atob(body.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json);
  } catch {
    return null;
  }
}

export default function Tokens() {
  const token = getToken() || "";
  const claims = decode(token);
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(token);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <>
      <div className="head"><h2>Tokens</h2></div>
      <div className="panel">
        <h3>Your bearer token</h3>
        <div className="hint">
          This HMAC token is what an LLM, script, or another service carries to call
          SHABD on your behalf. It is signed with the engine-wide stable secret.
        </div>
        <pre style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{token || "no token"}</pre>
        <button onClick={copy}>{copied ? "Copied ✓" : "Copy token"}</button>
      </div>
      {claims && (
        <div className="panel">
          <h3>Claims</h3>
          <table>
            <tbody>
              <tr><td>subject</td><td><code>{claims.sub}</code></td></tr>
              <tr><td>scopes</td><td>{(claims.scopes || []).map((s) => <span key={s} className="tag info" style={{ marginRight: 4 }}>{s}</span>)}</td></tr>
              <tr><td>issued</td><td>{claims.iat ? new Date(claims.iat * 1000).toLocaleString() : "—"}</td></tr>
              <tr><td>expires</td><td>{claims.exp ? new Date(claims.exp * 1000).toLocaleString() : "—"}</td></tr>
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
