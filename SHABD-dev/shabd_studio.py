"""
shabd_studio.py — a visual, drag-and-drop chatbot builder (zero deps).

Think n8n / Flowise, but pure standard library and backed by your SHABD
stack. The heavy server UI (`shabd_ui.py`) stays the control plane —
this Studio is a light, white-themed canvas where a non-developer drags
tools, agents and flows onto a board, wires them into an Assistant,
gives it a system prompt, tests it live, and clicks **Publish**.

What you get when you publish a bot
===================================

  * A ready API:           POST /chat/<bot>   {"message": "..."}
  * A one-line embed:      <script src=".../embed/<bot>.js"></script>
  * A hosted chat page:    /c/<bot>
  * Every chat turn lands in the Grimoire hash-chain — a tamper-evident
    conversation log (compliance-grade).

Revolutionary bit
=================

A published bot itself becomes a palette node, so you can drag a bot
into another bot — composable "bot of bots" — all auditable.

Run it
======

Alongside the main UI (shares the same backend + login):

    python -m shabd_ui --port 8080 --studio-port 8095

Then open  http://localhost:8095/  (sign in on :8080 first).

The Studio shares the UIServer's session store, so the cookie set by
the main UI's login is accepted here too (same host).
"""
from __future__ import annotations

import http.server
import json
import logging
import socketserver
import typing as t
import urllib.parse
from http.cookies import SimpleCookie

log = logging.getLogger("shabd.studio")

__all__ = ["StudioServer", "run"]


# ============================================================================
# The Studio HTML — light theme, vanilla drag-drop canvas
# ============================================================================

_STUDIO_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><title>SHABD Studio</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* { box-sizing:border-box; }
:root{
  --bg:#ffffff; --panel:#f8fafc; --line:#e2e8f0; --line2:#cbd5e1;
  --text:#1e293b; --dim:#64748b; --accent:#7c3aed; --accent2:#a78bfa;
  --ok:#16a34a; --wire:#a78bfa;
}
html,body{ margin:0; height:100%; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  color:var(--text); background:var(--bg); }
body{ display:flex; flex-direction:column; height:100vh; overflow:hidden; }
.topbar{ display:flex; align-items:center; gap:12px; padding:10px 16px;
  border-bottom:1px solid var(--line); background:var(--panel); flex-shrink:0; }
.topbar .logo{ font-weight:700; color:var(--accent); font-size:18px; }
.topbar input{ padding:8px 12px; border:1px solid var(--line2); border-radius:8px;
  font-size:14px; }
.topbar .sp{ flex:1; }
button{ background:var(--accent); color:#fff; border:0; border-radius:8px;
  padding:8px 16px; font-size:14px; font-weight:600; cursor:pointer; }
button.ghost{ background:#fff; color:var(--text); border:1px solid var(--line2); }
button:hover{ filter:brightness(1.05); }
.main{ flex:1; display:flex; min-height:0; }
.palette{ width:230px; border-right:1px solid var(--line); background:var(--panel);
  overflow-y:auto; padding:12px; flex-shrink:0; }
.palette h4{ margin:14px 0 6px; font-size:11px; text-transform:uppercase;
  letter-spacing:1px; color:var(--dim); }
.chip{ display:block; padding:9px 12px; margin:6px 0; background:#fff;
  border:1px solid var(--line2); border-radius:10px; font-size:13px; cursor:grab;
  box-shadow:0 1px 2px rgba(0,0,0,.04); }
.chip:active{ cursor:grabbing; }
.chip .k{ font-size:10px; color:var(--dim); text-transform:uppercase; }
.canvas-wrap{ flex:1; position:relative; overflow:hidden;
  background:
    radial-gradient(circle, #e2e8f0 1px, transparent 1px) 0 0 / 22px 22px,
    #fdfdff; }
#wires{ position:absolute; inset:0; width:100%; height:100%; pointer-events:none; }
.node{ position:absolute; min-width:150px; background:#fff; border:1px solid var(--line2);
  border-radius:12px; box-shadow:0 4px 14px rgba(0,0,0,.07); padding:10px 12px;
  font-size:13px; cursor:grab; user-select:none; }
.node.assistant{ border:2px solid var(--accent); min-width:190px; }
.node .ntype{ font-size:10px; color:var(--dim); text-transform:uppercase; letter-spacing:1px; }
.node .nname{ font-weight:600; margin-top:2px; }
.node .x{ position:absolute; top:6px; right:8px; color:var(--dim); cursor:pointer;
  font-size:12px; }
.inspector{ width:330px; border-left:1px solid var(--line); background:var(--panel);
  display:flex; flex-direction:column; flex-shrink:0; }
.inspector .sec{ padding:14px; border-bottom:1px solid var(--line); }
.inspector h4{ margin:0 0 8px; font-size:13px; }
.inspector label{ display:block; font-size:11px; color:var(--dim); margin:8px 0 4px;
  text-transform:uppercase; letter-spacing:.5px; }
.inspector input, .inspector textarea{ width:100%; padding:8px 10px;
  border:1px solid var(--line2); border-radius:8px; font-size:13px; font-family:inherit; }
.inspector textarea{ min-height:70px; resize:vertical; }
.chatbox{ flex:1; display:flex; flex-direction:column; min-height:0; }
.msgs{ flex:1; overflow-y:auto; padding:12px; display:flex; flex-direction:column; gap:8px; }
.msg{ padding:8px 12px; border-radius:12px; font-size:13px; max-width:85%; }
.msg.user{ align-self:flex-end; background:var(--accent); color:#fff; }
.msg.bot{ align-self:flex-start; background:#fff; border:1px solid var(--line2); }
.msg.bot pre{ background:#0f172a; color:#e2e8f0; padding:8px 10px; border-radius:8px;
  font-size:12px; overflow-x:auto; margin:6px 0; }
.msg.bot code{ background:#f1f5f9; padding:1px 5px; border-radius:4px; font-size:12px; }
.msg.bot ul{ margin:6px 0; padding-left:18px; }
.msg.bot a{ color:var(--accent); }
.msg.sys{ align-self:center; color:var(--dim); font-size:11px; }
.chat-in{ display:flex; gap:8px; padding:10px; border-top:1px solid var(--line); }
.chat-in input{ flex:1; padding:9px 12px; border:1px solid var(--line2); border-radius:8px; }
.tag{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px;
  background:#ede9fe; color:var(--accent); }
.publish-out{ padding:12px; }
.publish-out pre{ background:#0f172a; color:#e2e8f0; padding:10px; border-radius:8px;
  font-size:11px; overflow-x:auto; white-space:pre-wrap; word-break:break-all; }
.empty{ color:var(--dim); font-size:12px; text-align:center; padding:20px; }
</style></head><body>
<div class="topbar">
  <span class="logo">🎨 SHABD Studio</span>
  <input id="botName" placeholder="my_assistant" value="my_assistant">
  <span class="sp"></span>
  <select id="botPicker" class="ghost" style="padding:8px;border-radius:8px;border:1px solid var(--line2)"></select>
  <button class="ghost" onclick="newBot()">New</button>
  <button class="ghost" onclick="saveBot(false)">Save</button>
  <button onclick="saveBot(true)">Publish</button>
</div>
<div class="main">
  <div class="palette" id="palette"><div class="empty">Loading…</div></div>
  <div class="canvas-wrap" id="canvas">
    <svg id="wires"></svg>
  </div>
  <div class="inspector">
    <div class="sec" id="inspector">
      <h4>Inspector</h4>
      <div class="empty">Click the Assistant node to set its prompt, or drag tools from the left.</div>
    </div>
    <div class="chatbox">
      <div class="sec" style="border-bottom:none;padding-bottom:6px"><h4>Live test</h4></div>
      <div class="msgs" id="msgs"><div class="empty">Save the bot, then chat to test it.</div></div>
      <div class="chat-in">
        <input id="chatIn" placeholder="Type a message…" onkeydown="if(event.key==='Enter')sendChat()">
        <button onclick="sendChat()">Send</button>
      </div>
    </div>
  </div>
</div>
<script>
const CSRF = "__CSRF__";
const ORIGIN = location.origin;
let nodes = {};      // id -> {id,type,ref,x,y,el}
let nextId = 1;
let assistantId = null;
let published = null;

async function api(path, opts={}) {
  opts.headers = Object.assign({'X-CSRF':CSRF,'Accept':'application/json'}, opts.headers||{});
  if (opts.body && typeof opts.body!=='string'){ opts.headers['Content-Type']='application/json'; opts.body=JSON.stringify(opts.body); }
  const r = await fetch(path, opts); const t = await r.text();
  try { return {status:r.status, body:JSON.parse(t)}; } catch { return {status:r.status, body:t}; }
}

// ---------- palette ----------
async function loadPalette() {
  const r = await api('/api/palette');
  const p = r.body;
  const cats = [
    ['Tools', 'tool', p.tools||[]],
    ['Agents', 'agent', p.agents||[]],
    ['Flows', 'flow', p.flows||[]],
    ['Bots', 'bot', p.bots||[]],
  ];
  let html = '';
  for (const [title, kind, items] of cats) {
    html += '<h4>'+title+'</h4>';
    if (!items.length) html += '<div class="empty" style="padding:6px">none</div>';
    items.forEach(it => {
      const name = typeof it==='string'?it:it.name;
      html += '<div class="chip" draggable="true" data-kind="'+kind+'" data-ref="'+name+'">'+
        '<div class="k">'+kind+'</div>'+name+'</div>';
    });
  }
  const pal = document.getElementById('palette');
  pal.innerHTML = html;
  pal.querySelectorAll('.chip').forEach(c => {
    c.addEventListener('dragstart', e => {
      e.dataTransfer.setData('kind', c.dataset.kind);
      e.dataTransfer.setData('ref', c.dataset.ref);
    });
  });
  // bot picker
  const sel = document.getElementById('botPicker');
  sel.innerHTML = '<option value="">— open a bot —</option>' +
    (p.bots||[]).map(b => '<option value="'+b.name+'">'+b.name+'</option>').join('');
  sel.onchange = () => { if (sel.value) openBot(sel.value); };
}

// ---------- canvas ----------
const canvas = document.getElementById('canvas');
canvas.addEventListener('dragover', e => e.preventDefault());
canvas.addEventListener('drop', e => {
  e.preventDefault();
  const kind = e.dataTransfer.getData('kind');
  const ref = e.dataTransfer.getData('ref');
  if (!kind || !ref) return;
  const rect = canvas.getBoundingClientRect();
  addNode(kind, ref, e.clientX-rect.left-70, e.clientY-rect.top-20, true);
});

function addAssistant() {
  const rect = canvas.getBoundingClientRect();
  const id = addNode('assistant', 'Assistant',
    rect.width/2-95, rect.height/2-30, false);
  assistantId = id;
  selectNode(id);
}

function addNode(type, ref, x, y, wire) {
  const id = 'n'+(nextId++);
  const el = document.createElement('div');
  el.className = 'node'+(type==='assistant'?' assistant':'');
  el.style.left = Math.max(4,x)+'px'; el.style.top = Math.max(4,y)+'px';
  el.innerHTML = (type==='assistant'?'':'<span class="x" onclick="removeNode(\''+id+'\')">✕</span>')+
    '<div class="ntype">'+type+'</div><div class="nname">'+
    (type==='assistant'?'🤖 Assistant':ref)+'</div>';
  canvas.appendChild(el);
  nodes[id] = {id,type,ref,x:Math.max(4,x),y:Math.max(4,y),el};
  makeDraggable(id);
  el.addEventListener('click', ev => { if(!ev.target.classList.contains('x')) selectNode(id); });
  redrawWires();
  return id;
}

function removeNode(id) {
  if (!nodes[id] || nodes[id].type==='assistant') return;
  nodes[id].el.remove(); delete nodes[id]; redrawWires();
}

function makeDraggable(id) {
  const n = nodes[id]; let sx, sy, ox, oy, drag=false;
  n.el.addEventListener('pointerdown', e => {
    if (e.target.classList.contains('x')) return;
    drag=true; n.el.setPointerCapture(e.pointerId);
    sx=e.clientX; sy=e.clientY; ox=n.x; oy=n.y; n.el.style.cursor='grabbing';
  });
  n.el.addEventListener('pointermove', e => {
    if(!drag) return;
    n.x=Math.max(4,ox+(e.clientX-sx)); n.y=Math.max(4,oy+(e.clientY-sy));
    n.el.style.left=n.x+'px'; n.el.style.top=n.y+'px'; redrawWires();
  });
  n.el.addEventListener('pointerup', e => { drag=false; n.el.style.cursor='grab'; });
}

function center(n){ return {x:n.x+n.el.offsetWidth/2, y:n.y+n.el.offsetHeight/2}; }
function redrawWires() {
  const svg = document.getElementById('wires');
  if (!assistantId || !nodes[assistantId]) { svg.innerHTML=''; return; }
  const a = center(nodes[assistantId]);
  let paths='';
  for (const id in nodes) {
    if (id===assistantId) continue;
    const c = center(nodes[id]);
    const mx=(c.x+a.x)/2;
    paths += '<path d="M '+c.x+' '+c.y+' C '+mx+' '+c.y+' '+mx+' '+a.y+' '+a.x+' '+a.y+
      '" stroke="var(--wire)" stroke-width="2" fill="none" opacity="0.7"/>';
  }
  svg.innerHTML = paths;
}

// ---------- inspector ----------
let cfg = { system:'You are a helpful assistant.', greeting:'Hi! How can I help?', force_tools:false };
function selectNode(id) {
  const n = nodes[id]; const ins = document.getElementById('inspector');
  if (n.type==='assistant') {
    ins.innerHTML = '<h4>Assistant</h4>'+
      '<label>System prompt</label><textarea id="ins-sys">'+esc(cfg.system)+'</textarea>'+
      '<label>Greeting</label><input id="ins-greet" value="'+esc(cfg.greeting)+'">'+
      '<label style="margin-top:10px"><input type="checkbox" id="ins-force" '+(cfg.force_tools?'checked':'')+'> force tool use</label>'+
      '<div style="margin-top:10px;color:var(--dim);font-size:12px">Drag tools/agents from the left and wire them in. Connected: '+connectedCount()+'</div>';
    document.getElementById('ins-sys').addEventListener('input', e=>cfg.system=e.target.value);
    document.getElementById('ins-greet').addEventListener('input', e=>cfg.greeting=e.target.value);
    document.getElementById('ins-force').addEventListener('change', e=>cfg.force_tools=e.target.checked);
  } else {
    ins.innerHTML = '<h4>'+n.type+'</h4><div class="nname">'+esc(n.ref)+'</div>'+
      '<div style="margin-top:8px;color:var(--dim);font-size:12px">This '+n.type+' is wired into the Assistant. Drag it to reposition or ✕ to remove.</div>';
  }
}
function connectedCount(){ return Object.values(nodes).filter(n=>n.type!=='assistant').length; }
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }

// ---------- collect + save ----------
function collect() {
  const tools=[], agents=[], graph={nodes:[]};
  for (const id in nodes) {
    const n=nodes[id];
    graph.nodes.push({type:n.type, ref:n.ref, x:n.x, y:n.y});
    if (n.type==='tool') tools.push(n.ref);
    if (n.type==='agent') agents.push(n.ref);
    // flows/bots: treated as agents-of-tools later; kept in graph
  }
  return { name:document.getElementById('botName').value.trim(),
    system:cfg.system, greeting:cfg.greeting, force_tools:cfg.force_tools,
    tools, agents, graph };
}

async function saveBot(publish) {
  const body = collect();
  if (!body.name) { alert('Give the bot a name'); return; }
  const r = await api('/api/chatbots/save', {method:'POST', body});
  if (r.status>=400 || !r.body.ok) { alert(r.body.error||'save failed'); return; }
  addSys(publish ? 'Published ✓' : 'Saved ✓');
  loadPalette();
  if (publish) showPublish(body.name);
}

function showPublish(name) {
  const ins = document.getElementById('inspector');
  const embed = '<script src="'+ORIGIN+'/embed/'+name+'.js"><\/script>';
  ins.innerHTML = '<h4>🎉 Published: '+name+'</h4>'+
    '<div class="publish-out">'+
    '<label>API — call from anywhere</label>'+
    '<pre>curl -X POST '+ORIGIN+'/chat/'+name+' \\\n  -H "Content-Type: application/json" \\\n  -d \'{"message":"hello"}\'</pre>'+
    '<label>Embed on any website</label>'+
    '<pre>'+esc(embed)+'</pre>'+
    '<label>Hosted page</label>'+
    '<pre>'+ORIGIN+'/c/'+name+'</pre>'+
    '<a class="tag" href="/c/'+name+'" target="_blank">open hosted chat ↗</a>'+
    '</div>';
}

// ---------- open existing ----------
async function openBot(name) {
  const r = await api('/api/chatbots/'+encodeURIComponent(name));
  if (r.status>=400) { alert('not found'); return; }
  const b = r.body;
  // reset
  Object.values(nodes).forEach(n=>n.el.remove()); nodes={}; assistantId=null;
  document.getElementById('botName').value = b.name;
  cfg = { system:b.system, greeting:b.greeting, force_tools:b.force_tools };
  addAssistant();
  const g = b.graph && b.graph.nodes ? b.graph.nodes : [];
  g.forEach(nd => { if (nd.type!=='assistant') addNode(nd.type, nd.ref, nd.x, nd.y, true); });
  // ensure tools/agents present even if graph empty
  if (!g.length) {
    (b.tools||[]).forEach((tname,i)=>addNode('tool', tname, 60, 60+i*60, true));
    (b.agents||[]).forEach((aname,i)=>addNode('agent', aname, 60, 300+i*60, true));
  }
  addSys('Loaded '+name);
}
function newBot(){
  Object.values(nodes).forEach(n=>n.el.remove()); nodes={}; assistantId=null;
  cfg={system:'You are a helpful assistant.',greeting:'Hi! How can I help?',force_tools:false};
  document.getElementById('botName').value='my_assistant';
  document.getElementById('msgs').innerHTML='<div class="empty">Save the bot, then chat to test it.</div>';
  addAssistant();
}

// ---------- markdown (tiny, safe) ----------
function mdEsc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function md(s){
  const B=[]; s=String(s);
  s=s.replace(/```([\s\S]*?)```/g,function(m,c){B.push(c.replace(/^\n/,''));return 'CBLK'+(B.length-1)+'KBLC';});
  s=mdEsc(s);
  s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
  s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  s=s.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
  s=s.replace(/^#{1,3} (.*)$/gm,'<b>$1</b>');
  s=s.replace(/^\s*[-*] (.*)$/gm,'<li>$1</li>');
  s=s.replace(/(?:<li>.*?<\/li>\n?)+/g,function(m){return '<ul>'+m.replace(/\n/g,'')+'</ul>';});
  s=s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank">$1</a>');
  s=s.replace(/\n/g,'<br>');
  s=s.replace(/CBLK(\d+)KBLC/g,function(m,i){return '<pre style="white-space:pre-wrap">'+mdEsc(B[i])+'</pre>';});
  return s;
}
// ---------- live test ----------
let history=[];
function addMsg(role,text){
  const m=document.getElementById('msgs');
  if (m.querySelector('.empty')) m.innerHTML='';
  const d=document.createElement('div'); d.className='msg '+role;
  if (role==='bot') d.innerHTML=md(text); else d.textContent=text;
  m.appendChild(d);
  m.scrollTop=m.scrollHeight;
}
function addSys(t){ addMsg('sys', t); }
async function sendChat(){
  const inp=document.getElementById('chatIn'); const msg=inp.value.trim(); if(!msg) return;
  const name=document.getElementById('botName').value.trim();
  inp.value=''; addMsg('user', msg); history.push({role:'user',text:msg});
  // auto-save draft so the server has the latest config
  await saveBot(false);
  const r = await api('/api/chatbots/'+encodeURIComponent(name)+'/test',
    {method:'POST', body:{message:msg, history}});
  const reply = r.body && r.body.ok ? r.body.reply : (r.body.error||'error');
  addMsg('bot', reply); history.push({role:'assistant',text:reply});
}

// ---------- boot ----------
loadPalette().then(addAssistant);
</script>
</body></html>"""


# A minimal hosted chat page for a published bot.
_HOSTED_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>__NAME__</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;font-family:system-ui,sans-serif;background:#f8fafc;color:#1e293b;
  display:flex;flex-direction:column;height:100vh;}
.hdr{padding:14px 18px;background:#fff;border-bottom:1px solid #e2e8f0;font-weight:600;}
.msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;}
.m{padding:10px 14px;border-radius:14px;max-width:80%;font-size:14px;}
.m.user{align-self:flex-end;background:#7c3aed;color:#fff;}
.m.bot{align-self:flex-start;background:#fff;border:1px solid #e2e8f0;}
.in{display:flex;gap:8px;padding:12px;background:#fff;border-top:1px solid #e2e8f0;}
.in input{flex:1;padding:11px 14px;border:1px solid #cbd5e1;border-radius:10px;font-size:14px;}
.in button{background:#7c3aed;color:#fff;border:0;border-radius:10px;padding:11px 18px;font-weight:600;cursor:pointer;}
</style></head><body>
<div class="hdr">🤖 __NAME__</div>
<div class="msgs" id="m"></div>
<div class="in"><input id="i" placeholder="Type a message…"
  onkeydown="if(event.key==='Enter')send()"><button onclick="send()">Send</button></div>
<script>
const NAME="__NAME__"; let history=[];
function mdEsc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}function md(s){var B=[];s=String(s);s=s.replace(/```([\s\S]*?)```/g,function(m,c){B.push(c.replace(/^\n/,''));return 'CBLK'+(B.length-1)+'KBLC';});s=mdEsc(s);s=s.replace(/`([^`]+)`/g,'<code>$1</code>');s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');s=s.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');s=s.replace(/^#{1,3} (.*)$/gm,'<b>$1</b>');s=s.replace(/^\s*[-*] (.*)$/gm,'<li>$1</li>');s=s.replace(/(?:<li>.*?<\/li>\n?)+/g,function(m){return '<ul>'+m.replace(/\n/g,'')+'</ul>';});s=s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank">$1</a>');s=s.replace(/\n/g,'<br>');s=s.replace(/CBLK(\d+)KBLC/g,function(m,i){return '<pre style="white-space:pre-wrap">'+mdEsc(B[i])+'</pre>';});return s;}
function add(role,text){var m=document.getElementById('m');var d=document.createElement('div');d.className='m '+role;if(role==='bot')d.innerHTML=md(text);else d.textContent=text;m.appendChild(d);m.scrollTop=m.scrollHeight;}
add('bot', "__GREETING__");
async function send(){const i=document.getElementById('i');const msg=i.value.trim();if(!msg)return;
  i.value='';add('user',msg);history.push({role:'user',text:msg});
  const r=await fetch('/chat/'+NAME,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:msg,history})});
  const b=await r.json();const reply=b.reply||b.error||'…';
  add('bot',reply);history.push({role:'assistant',text:reply});}
</script></body></html>"""


# The embeddable widget — a floating chat bubble injected by one <script>.
_EMBED_JS = r"""(function(){
  var NAME="__NAME__", ORIGIN="__ORIGIN__", history=[];
  var css = "#shabd-bub{position:fixed;bottom:20px;right:20px;width:56px;height:56px;border-radius:50%;background:#7c3aed;color:#fff;font-size:24px;border:0;cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.2);z-index:999999}"+
    "#shabd-win{position:fixed;bottom:88px;right:20px;width:340px;height:460px;background:#fff;border:1px solid #e2e8f0;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.2);display:none;flex-direction:column;overflow:hidden;z-index:999999;font-family:system-ui,sans-serif}"+
    "#shabd-win .h{padding:12px;background:#7c3aed;color:#fff;font-weight:600}"+
    "#shabd-win .m{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px}"+
    "#shabd-win .b{padding:8px 12px;border-radius:12px;font-size:13px;max-width:85%}"+
    "#shabd-win .b.u{align-self:flex-end;background:#7c3aed;color:#fff}"+
    "#shabd-win .b.a{align-self:flex-start;background:#f1f5f9}"+
    "#shabd-win .i{display:flex;gap:6px;padding:8px;border-top:1px solid #e2e8f0}"+
    "#shabd-win .i input{flex:1;padding:8px;border:1px solid #cbd5e1;border-radius:8px}"+
    "#shabd-win .i button{background:#7c3aed;color:#fff;border:0;border-radius:8px;padding:8px 12px;cursor:pointer}";
  var s=document.createElement('style');s.textContent=css;document.head.appendChild(s);
  var bub=document.createElement('button');bub.id='shabd-bub';bub.textContent='💬';document.body.appendChild(bub);
  var win=document.createElement('div');win.id='shabd-win';
  win.innerHTML='<div class="h">🤖 '+NAME+'</div><div class="m" id="shabd-m"></div>'+
    '<div class="i"><input id="shabd-in" placeholder="Type…"><button id="shabd-send">Send</button></div>';
  document.body.appendChild(win);
  bub.onclick=function(){win.style.display=win.style.display==='flex'?'none':'flex';};
  function mdEsc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}function md(s){var B=[];s=String(s);s=s.replace(/```([\s\S]*?)```/g,function(m,c){B.push(c.replace(/^\n/,''));return 'CBLK'+(B.length-1)+'KBLC';});s=mdEsc(s);s=s.replace(/`([^`]+)`/g,'<code>$1</code>');s=s.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');s=s.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');s=s.replace(/^#{1,3} (.*)$/gm,'<b>$1</b>');s=s.replace(/^\s*[-*] (.*)$/gm,'<li>$1</li>');s=s.replace(/(?:<li>.*?<\/li>\n?)+/g,function(m){return '<ul>'+m.replace(/\n/g,'')+'</ul>';});s=s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank">$1</a>');s=s.replace(/\n/g,'<br>');s=s.replace(/CBLK(\d+)KBLC/g,function(m,i){return '<pre style="white-space:pre-wrap">'+mdEsc(B[i])+'</pre>';});return s;}
  function add(role,text){var m=document.getElementById('shabd-m');var d=document.createElement('div');d.className='b '+(role==='user'?'u':'a');if(role!=='user')d.innerHTML=md(text);else d.textContent=text;m.appendChild(d);m.scrollTop=m.scrollHeight;}
  add('a',"__GREETING__");
  function send(){var i=document.getElementById('shabd-in');var msg=i.value.trim();if(!msg)return;
    i.value='';add('user',msg);history.push({role:'user',text:msg});
    fetch(ORIGIN+'/chat/'+NAME,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg,history})}).then(function(r){return r.json();}).then(function(b){
      var reply=b.reply||b.error||'…';add('bot',reply);history.push({role:'assistant',text:reply});});}
  document.getElementById('shabd-send').onclick=send;
  document.getElementById('shabd-in').addEventListener('keydown',function(e){if(e.key==='Enter')send();});
})();"""


# ============================================================================
# StudioServer
# ============================================================================

class StudioServer:
    """A visual chatbot builder that uses a `UIServer` as its backend."""

    def __init__(self, ui: t.Any, *, bind: str = "127.0.0.1",
                 port: int = 8095):
        self.ui = ui
        self.bind = bind
        self.port = port

    def serve(self) -> None:
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        handler = _make_handler(self)
        srv = socketserver.ThreadingTCPServer((self.bind, self.port), handler)
        srv.daemon_threads = True
        log.info("SHABD Studio on %s:%s", self.bind, self.port)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()


def _make_handler(studio: StudioServer):
    ui = studio.ui

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        # -- helpers --
        def _body(self) -> bytes:
            n = int(self.headers.get("content-length") or 0)
            return self.rfile.read(n) if n else b""

        def _json(self, status: int, payload: t.Any) -> None:
            b = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b)

        def _html(self, status: int, html: str,
                  ctype: str = "text/html; charset=utf-8") -> None:
            b = html.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b)

        def _session(self):
            ck = SimpleCookie(self.headers.get("cookie", ""))
            m = ck.get("shabd_sid")
            if not m:
                return None
            return ui.sessions.get(m.value)

        def _origin(self) -> str:
            host = self.headers.get("host", f"{studio.bind}:{studio.port}")
            return f"http://{host}"

        # -- routing --
        def do_GET(self):  # noqa: N802
            try:
                self._route_get()
            except Exception:
                log.exception("studio GET error")
                self._json(500, {"error": "internal error"})

        def do_POST(self):  # noqa: N802
            try:
                self._route_post()
            except Exception:
                log.exception("studio POST error")
                self._json(500, {"error": "internal error"})

        def _route_get(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/healthz":
                return self._json(200, {"ok": True})
            # public hosted page + embed
            if path.startswith("/c/"):
                return self._hosted(urllib.parse.unquote(path[3:]))
            if path.startswith("/embed/") and path.endswith(".js"):
                name = urllib.parse.unquote(path[len("/embed/"):-3])
                return self._embed(name)
            # everything else needs a session
            sess = self._session()
            if not sess:
                return self._html(
                    200,
                    "<h2 style='font-family:sans-serif'>Please sign in "
                    "on the main SHABD UI first, then reload this "
                    "Studio.</h2>")
            if path == "/":
                return self._html(
                    200, _STUDIO_HTML.replace("__CSRF__", sess.csrf))
            if path == "/api/palette":
                return self._json(200, {
                    "tools": [n for n, s in ui.app._spells.items()
                              if "chain" not in (s.tags or [])
                              and not n.startswith("__")],
                    "agents": list(ui._agents.keys()),
                    "flows": [f["name"] for f in ui.list_flows()],
                    "bots": ui.list_chatbots(),
                })
            if path == "/api/chatbots":
                return self._json(200,
                                   {"chatbots": ui.list_chatbots()})
            if path.startswith("/api/chatbots/"):
                name = urllib.parse.unquote(path[len("/api/chatbots/"):])
                bot = ui.get_chatbot(name)
                if not bot:
                    return self._json(404, {"error": "not found"})
                return self._json(200, bot)
            self._json(404, {"error": "not found"})

        def _route_post(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            # public chat endpoint
            if path.startswith("/chat/"):
                return self._chat(urllib.parse.unquote(path[len("/chat/"):]))
            sess = self._session()
            if not sess:
                return self._json(401, {"error": "not signed in"})
            if path == "/api/chatbots/save":
                return self._save(sess)
            if path.endswith("/delete") and path.startswith("/api/chatbots/"):
                name = urllib.parse.unquote(
                    path[len("/api/chatbots/"):-len("/delete")])
                return self._json(200, ui.delete_chatbot(sess, name))
            if path.endswith("/test") and path.startswith("/api/chatbots/"):
                name = urllib.parse.unquote(
                    path[len("/api/chatbots/"):-len("/test")])
                return self._test(sess, name)
            self._json(404, {"error": "not found"})

        # -- handlers --
        def _save(self, sess):
            try:
                body = json.loads(self._body() or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            try:
                bot = ui.save_chatbot(
                    sess,
                    name=(body.get("name") or "").strip(),
                    system=body.get("system") or "",
                    greeting=body.get("greeting") or "",
                    tools=body.get("tools") or [],
                    agents=body.get("agents") or [],
                    graph=body.get("graph") or {},
                    force_tools=bool(body.get("force_tools", False)))
                self._json(200, {"ok": True, **bot})
            except Exception as e:
                self._json(getattr(e, "status", 400),
                           {"error": getattr(e, "message", str(e))})

        def _test(self, sess, name):
            try:
                body = json.loads(self._body() or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            try:
                res = ui.run_chatbot(
                    sess, name=name,
                    message=body.get("message") or "",
                    history=body.get("history") or [])
                self._json(200, res)
            except Exception as e:
                self._json(getattr(e, "status", 400),
                           {"error": getattr(e, "message", str(e))})

        def _chat(self, name):
            """Public chat turn. Token optional (subject recorded)."""
            try:
                body = json.loads(self._body() or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            subject = "web"
            auth = self.headers.get("authorization", "")
            if auth:
                tok = auth.removeprefix("Bearer ").strip()
                try:
                    payload = ui.app.tokens.verify(tok)
                    subject = payload.get("sub", "web")
                except Exception:
                    return self._json(401, {"error": "invalid token"})
            from shabd_ui import Session
            pseudo = Session(sid="studio-chat", username=subject,
                             roles=["user"], access_token="web")
            try:
                res = ui.run_chatbot(
                    pseudo, name=name,
                    message=body.get("message") or "",
                    history=body.get("history") or [])
                self._json(200, res)
            except Exception as e:
                self._json(getattr(e, "status", 404),
                           {"error": getattr(e, "message", str(e))})

        def _hosted(self, name):
            bot = ui.get_chatbot(name)
            if not bot:
                return self._html(404, "<h2>Bot not found</h2>")
            html = (_HOSTED_HTML
                    .replace("__NAME__", name)
                    .replace("__GREETING__",
                             (bot.get("greeting") or "Hi!")
                             .replace('"', "'")))
            self._html(200, html)

        def _embed(self, name):
            bot = ui.get_chatbot(name)
            if not bot:
                return self._html(404, "// bot not found",
                                  "application/javascript")
            js = (_EMBED_JS
                  .replace("__NAME__", name)
                  .replace("__ORIGIN__", self._origin())
                  .replace("__GREETING__",
                           (bot.get("greeting") or "Hi!")
                           .replace('"', "'")))
            self._html(200, js, "application/javascript; charset=utf-8")

    return H


def run(ui: t.Any, *, bind: str = "127.0.0.1", port: int = 8095) -> None:
    StudioServer(ui, bind=bind, port=port).serve()
