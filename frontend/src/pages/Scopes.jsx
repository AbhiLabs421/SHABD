import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function Scopes() {
  const [spells, setSpells] = useState([]);
  useEffect(() => {
    api.spells().then((d) => setSpells(d.spells || []));
  }, []);
  return (
    <>
      <div className="head"><h2>Scopes</h2></div>
      <div className="panel">
        <div className="hint" style={{ marginBottom: 10 }}>
          Which scope a caller's token must carry to invoke each spell. Tokens are
          minted with scopes at login; a spell with no scopes is open to any signed-in caller.
        </div>
        <table>
          <thead><tr><th>spell</th><th>required scopes</th><th>tags</th></tr></thead>
          <tbody>
            {spells.map((s) => (
              <tr key={s.name}>
                <td><code>{s.name}</code></td>
                <td>
                  {(s.scopes && s.scopes.length)
                    ? s.scopes.map((x) => <span key={x} className="tag info" style={{ marginRight: 4 }}>{x}</span>)
                    : <span className="tag muted">open</span>}
                </td>
                <td>{(s.tags || []).join(", ")}</td>
              </tr>
            ))}
            {spells.length === 0 && <tr><td colSpan="3" className="empty">No spells.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
