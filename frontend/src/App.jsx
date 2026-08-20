import { Routes, Route, NavLink, Navigate, useNavigate } from "react-router-dom";
import { getToken, getUser, clearSession } from "./lib/api.js";
import Login from "./pages/Login.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Spells from "./pages/Spells.jsx";
import Grimoire from "./pages/Grimoire.jsx";
import Audit from "./pages/Audit.jsx";
import Agent from "./pages/Agent.jsx";
import Chains from "./pages/Chains.jsx";
import Orchestrator from "./pages/Orchestrator.jsx";
import Notary from "./pages/Notary.jsx";
import ClientConsole from "./pages/ClientConsole.jsx";
import Sources from "./pages/Sources.jsx";
import ApiDocs from "./pages/ApiDocs.jsx";
import Users from "./pages/Users.jsx";
import Tokens from "./pages/Tokens.jsx";
import Scopes from "./pages/Scopes.jsx";
import Builder from "./pages/Builder.jsx";
import Settings from "./pages/Settings.jsx";
import ThemeSwitcher from "./components/ThemeSwitcher.jsx";

// The "shabd_ui login pehle, phir baaki khulta hai" rule, in the UI.
function RequireAuth({ children }) {
  return getToken() ? children : <Navigate to="/login" replace />;
}

const BASE_NAV = [
  ["/", "📊", "Dashboard"],
  ["/spells", "✨", "Spells"],
  ["/grimoire", "🔗", "Grimoire"],
  ["/audit", "📜", "Audit Log"],
  ["/agent", "🤖", "Agent Lab"],
  ["/chains", "⛓️", "Spell Chains"],
  ["/orchestrator", "🎯", "Orchestrator"],
  ["/notary", "🤝", "Notary"],
  ["/client", "🌐", "Client Console"],
  ["/sources", "🔌", "Tool Sources"],
  ["/api-docs", "📖", "API Docs"],
];
const ADMIN_NAV = [
  ["/users", "👥", "Users"],
  ["/tokens", "🔑", "Tokens"],
  ["/scopes", "🛡️", "Scopes"],
];
const SUPER_NAV = [["/builder", "🏗️", "Builder"]];

function Shell({ children }) {
  const user = getUser();
  const roles = user?.roles || [];
  const isAdmin = roles.includes("admin") || roles.includes("superuser");
  const isSuper = roles.includes("superuser");
  const nav = useNavigate();
  const logout = () => {
    clearSession();
    nav("/login");
  };
  const cls = ({ isActive }) => "navlink" + (isActive ? " active" : "");
  const Item = ([to, icon, label]) => (
    <NavLink key={to} to={to} end={to === "/"} className={cls}>
      <span className="icon">{icon}</span> {label}
    </NavLink>
  );
  return (
    <div className="app">
      <nav className="sidebar">
        <div className="brand">
          <h1>🔮 SHABD</h1>
          <div className="who">{user?.username} · {roles.join(", ")}</div>
        </div>
        {BASE_NAV.map(Item)}
        {isAdmin && <div className="nav-section">Admin</div>}
        {isAdmin && ADMIN_NAV.map(Item)}
        {isSuper && SUPER_NAV.map(Item)}
        <div className="nav-section">System</div>
        <NavLink to="/settings" className={cls}><span className="icon">⚙️</span> Settings</NavLink>
        <div className="spacer" />
        <ThemeSwitcher />
        <div className="foot"><a onClick={logout} href="#">↩ Sign out</a></div>
      </nav>
      <main className="content">{children}</main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        path="/*"
        element={
          <RequireAuth>
            <Shell>
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/spells" element={<Spells />} />
                <Route path="/grimoire" element={<Grimoire />} />
                <Route path="/audit" element={<Audit />} />
                <Route path="/agent" element={<Agent />} />
                <Route path="/chains" element={<Chains />} />
                <Route path="/orchestrator" element={<Orchestrator />} />
                <Route path="/notary" element={<Notary />} />
                <Route path="/client" element={<ClientConsole />} />
                <Route path="/sources" element={<Sources />} />
                <Route path="/api-docs" element={<ApiDocs />} />
                <Route path="/users" element={<Users />} />
                <Route path="/tokens" element={<Tokens />} />
                <Route path="/scopes" element={<Scopes />} />
                <Route path="/builder" element={<Builder />} />
                <Route path="/settings" element={<Settings />} />
              </Routes>
            </Shell>
          </RequireAuth>
        }
      />
    </Routes>
  );
}
