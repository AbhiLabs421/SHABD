import { useState } from "react";
import { api } from "../lib/api.js";

export default function Notary() {
  const [root, setRoot] = useState(null);
  const [seq, setSeq] = useState(0);
  const [proof, setProof] = useState(null);
  const [error, setError] = useState("");

  const publish = async () => {
    setError("");
    try {
      setRoot((await api.notaryRoot()).root);
    } catch (e) {
      setError(e.message);
    }
  };
  const prove = async () => {
    setError("");
    setProof(null);
    try {
      setProof(await api.notaryInclusion(Number(seq)));
    } catch (e) {
      setError(e.message);
    }
  };

  return (
    <>
      <h2>Notary — cross-entity witness</h2>
      <div className="panel">
        <div className="hint">
          The Grimoire stops <b>you</b> from lying to yourself. The Notary stops two
          partners from lying to <b>each other</b>. Each side publishes a signed
          <b> root</b> (a snapshot of its chain head) and the other side counter-signs
          it. After that, neither can rewrite history without invalidating the
          witness the other holds. An <b>inclusion proof</b> lets a regulator verify
          "decision #N existed at that moment" offline, without seeing the rest of
          the chain.
        </div>
      </div>

      <div className="panel">
        <h3>1. Publish a root</h3>
        <button onClick={publish}>Publish current root</button>
        {root && (
          <pre>{JSON.stringify(
            { entity: root.entity, seq: root.seq, pages_count: root.pages_count, head: root.head },
            null, 2)}</pre>
        )}
      </div>

      <div className="panel">
        <h3>2. Prove a decision was in the chain</h3>
        <div className="row">
          <div style={{ flex: "0 0 120px" }}>
            <label>page seq</label>
            <input type="number" value={seq} onChange={(e) => setSeq(e.target.value)} />
          </div>
          <button onClick={prove}>Build inclusion proof</button>
        </div>
        {error && <div className="error">{error}</div>}
        {proof && (
          <>
            <div style={{ marginTop: 12 }}>
              {proof.verification?.ok ? (
                <span className="tag ok">Proof valid — decision {proof.verification.seq} was in the chain</span>
              ) : (
                <span className="tag err">Invalid: {proof.verification?.reason}</span>
              )}
            </div>
            <pre>{JSON.stringify(proof.verification, null, 2)}</pre>
          </>
        )}
      </div>
    </>
  );
}
