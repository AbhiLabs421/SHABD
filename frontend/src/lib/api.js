// api.js — the ONLY place the frontend talks to the backend.
// Every call goes to the gateway (/api/...). The bearer token from login is
// stored in localStorage and attached to every request.

const TOKEN_KEY = "shabd_token";
const USER_KEY = "shabd_user";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
export function getUser() {
  const raw = localStorage.getItem(USER_KEY);
  return raw ? JSON.parse(raw) : null;
}
export function setSession(token, user) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}
export function clearSession() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

async function request(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`/api${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!res.ok) {
    const msg =
      data?.detail?.message || data?.detail || data?.message || data?.error || res.statusText;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

export const api = {
  // auth (open)
  register: (username, password) =>
    request("POST", "/auth/register", { username, password }),
  login: (username, password) => request("POST", "/auth/login", { username, password }),

  // protected
  health: () => request("GET", "/health"),
  spells: () => request("GET", "/spells"),
  invoke: (name, args) => request("POST", `/spells/${name}`, args),
  grimoireVerify: () => request("GET", "/grimoire/verify"),
  grimoirePages: () => request("GET", "/grimoire/pages"),
  notaryRoot: () => request("GET", "/notary/root"),
  notaryInclusion: (seq) => request("POST", "/notary/inclusion", { seq }),
  agentConfig: () => request("GET", "/agent/config"),
  agentRun: (prompt) => request("POST", "/agent/run", { prompt }),
  users: () => request("GET", "/users"),

  // chains
  chains: () => request("GET", "/chains"),
  createChain: (name, pipeline) => request("POST", "/chains", { name, pipeline }),

  // orchestrator
  orchestratorClassify: (query) => request("POST", "/orchestrator/classify", { query }),
  orchestratorRun: (query) => request("POST", "/orchestrator/run", { query }),

  // tool sources
  sources: () => request("GET", "/sources"),
  connectSource: (body) => request("POST", "/sources/connect", body),

  // client console (outbound proxy)
  clientCall: (body) => request("POST", "/client/call", body),

  // builder
  buildSpell: (code) => request("POST", "/builder", { code }),
};
