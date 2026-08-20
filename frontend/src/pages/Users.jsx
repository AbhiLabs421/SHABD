import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function Users() {
  const [users, setUsers] = useState([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.users().then((d) => setUsers(d.users || [])).catch((e) => setErr(e.message));
  }, []);

  return (
    <>
      <div className="head"><h2>Users</h2></div>
      <div className="panel">
        <div className="hint" style={{ marginBottom: 10 }}>
          The user store lives inside the users_service Grimoire chain — every
          register/login is a signed, tamper-evident page. First account = superuser.
        </div>
        {err && <div className="error">{err}</div>}
        <table>
          <thead>
            <tr><th>username</th><th>roles</th><th>created</th><th>last login</th></tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.username}>
                <td>{u.username}</td>
                <td>{(u.roles || []).map((r) => <span key={r} className="tag muted" style={{ marginRight: 4 }}>{r}</span>)}</td>
                <td>{u.created_at ? new Date(u.created_at * 1000).toLocaleDateString() : "—"}</td>
                <td>{u.last_login_at ? new Date(u.last_login_at * 1000).toLocaleString() : "never"}</td>
              </tr>
            ))}
            {users.length === 0 && <tr><td colSpan="4" className="empty">No users.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
