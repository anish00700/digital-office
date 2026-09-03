/* Digital Office - front end.
 *
 * A pure viewer. All state lives in the daemon's SQLite file; this reloads a
 * snapshot on connect and then follows an SSE event stream, so closing the tab
 * costs nothing and reopening it replays the office exactly as it stands.
 */
'use strict';

const TILE = 40, W = 26, H = 16;
const CANVAS_W = W * TILE, CANVAS_H = H * TILE;

const S = {
  roster: [], byId: {}, agents: {}, tasks: [], approvals: [],
  spend: {}, backend: '', startedAt: 0, seq: 0
};
const V = { sprites: {}, flyers: [], feed: [], selected: null, hover: null, t: 0 };

const $ = (id) => document.getElementById(id);
const canvas = $('floor');
const ctx = canvas.getContext('2d');

/* ---------------------------------------------------------------- boot -- */

async function boot() {
  await loadState();
  wireUI();
  connect();
  requestAnimationFrame(frame);
  setInterval(tickClock, 1000);
}

async function loadState() {
  const state = await (await fetch('/api/state')).json();
  S.roster = state.roster;
  S.byId = Object.fromEntries(state.roster.map(r => [r.id, r]));
  S.tasks = state.tasks;
  S.approvals = state.approvals;
  S.spend = state.spend;
  S.backend = state.backend;
  S.auth = state.auth;
  S.startedAt = state.started_at;
  S.seq = state.seq;

  for (const r of S.roster) {
    S.agents[r.id] = { status: 'idle', detail: '' };
    V.sprites[r.id] = {
      x: r.desk[0], y: r.desk[1] + 0.8, bob: Math.random() * 6.28,
      state: 'idle', bubble: null, thought: '', tool: '', toolAt: 0
    };
  }
  for (const a of state.agents) {
    if (S.agents[a.id]) {
      S.agents[a.id] = a;
      V.sprites[a.id].state = a.status;
    }
  }
  for (const m of state.messages) {
    pushFeed({
      type: m.sender === 'user' ? 'user.message' : 'agent.message_user',
      agent_id: m.sender, ts: m.ts, payload: { text: m.body }
    }, true);
  }
  $('backend').textContent = state.backend;
  $('backendStat').title = 'Backend: ' + state.backend + ' - auth: ' + state.auth;
  renderBoard(); renderApprovals(); renderSpend(); renderFeed();
}

/* --------------------------------------------------------------- stream -- */

let es = null, retry = 0;

function connect() {
  if (es) es.close();
  es = new EventSource('/api/stream?since=' + S.seq);
  es.onopen = () => { retry = 0; setConn('live', 'live'); };
  es.onmessage = (e) => {
    let ev; try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.seq <= S.seq) return;
    S.seq = ev.seq;
    handle(ev);
  };
  es.onerror = () => {
    setConn('dead', 'reconnecting');
    es.close();
    retry = Math.min(retry + 1, 6);
    setTimeout(connect, 500 * 2 ** retry);
  };
}

function setConn(cls, text) {
  $('conn').querySelector('.dot').className = 'dot ' + cls;
  $('connText').textContent = text;
}

function handle(ev) {
  const sp = V.sprites[ev.agent_id];
  switch (ev.type) {
    case 'agent.status':
      if (S.agents[ev.agent_id]) {
        S.agents[ev.agent_id].status = ev.payload.status;
        S.agents[ev.agent_id].detail = ev.payload.detail || '';
      }
      if (sp) sp.state = ev.payload.status;
      break;
    case 'agent.say':
    case 'agent.message_user':
      if (sp) sp.bubble = { text: ev.payload.text, until: V.t + bubbleTime(ev.payload.text) };
      pushFeed(ev); break;
    case 'agent.thinking':
      if (sp) { sp.thought = ev.payload.text; sp.state = 'thinking'; }
      pushFeed(ev); break;
    case 'agent.tool':
      if (sp) { sp.tool = ev.payload.tool; sp.toolAt = V.t; }
      pushFeed(ev); break;
    case 'agent.error':
      pushFeed(ev); break;
    case 'handoff':
      flyer(ev.agent_id, ev.payload.to); break;
    case 'task.created':
    case 'task.updated':
      refreshTasks(); pushFeed(ev); break;
    case 'approval.requested':
    case 'approval.decided':
      refreshApprovals(); pushFeed(ev); break;
    case 'usage':
      if (ev.payload.spend) { S.spend.total = ev.payload.spend; renderSpend(); }
      break;
    case 'user.message':
      pushFeed(ev); break;
    case 'office.budget':
      pushFeed({ ...ev, type: 'agent.error', payload: { text: ev.payload.reason } });
      break;
  }
}

const bubbleTime = (t) => Math.min(14, 3.5 + (t || '').length / 22);

async function refreshTasks() {
  S.tasks = (await (await fetch('/api/tasks')).json()).tasks;
  renderBoard();
}

async function refreshApprovals() {
  const state = await (await fetch('/api/state')).json();
  S.approvals = state.approvals;
  renderApprovals();
}

/* ------------------------------------------------------------- rendering -- */

function fit() {
  const box = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(box.width * dpr);
  canvas.height = Math.round(box.height * dpr);
  const scale = Math.min(box.width / CANVAS_W, box.height / CANVAS_H);
  V.scale = scale;
  V.ox = (box.width - CANVAS_W * scale) / 2;
  V.oy = (box.height - CANVAS_H * scale) / 2;
  V.box = box;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

// Visible world bounds, so the floor can be painted past the room and the
// office reads as continuing beyond the frame instead of being letterboxed.
const wxAt = (sx) => (sx - V.ox) / (TILE * V.scale);
const wyAt = (sy) => (sy - V.oy) / (TILE * V.scale);

const px = (wx) => V.ox + wx * TILE * V.scale;
const py = (wy) => V.oy + wy * TILE * V.scale;
const ps = (n) => n * V.scale;

let last = 0;
function frame(ts) {
  const dt = Math.min(0.05, (ts - last) / 1000 || 0);
  last = ts; V.t += dt;
  fit();
  update(dt);
  draw();
  requestAnimationFrame(frame);
}

function update(dt) {
  for (const f of V.flyers) f.p += dt / f.dur;
  V.flyers = V.flyers.filter(f => f.p < 1);
  for (const id in V.sprites) {
    const sp = V.sprites[id];
    sp.bob += dt * (sp.state === 'working' ? 6 : 2);
    if (sp.bubble && V.t > sp.bubble.until) sp.bubble = null;
  }
}

function draw() {
  const box = canvas.getBoundingClientRect();
  ctx.clearRect(0, 0, box.width, box.height);
  drawFloor();
  drawWalls();
  drawFurniture();

  const order = [...S.roster].sort((a, b) => a.desk[1] - b.desk[1]);
  for (const r of order) drawDesk(r);
  for (const r of order) drawAgent(r);
  for (const r of order) drawBubble(r);
  drawFlyers();
}

function drawFloor() {
  const box = V.box;
  const x0 = Math.floor(wxAt(0)) - 1, x1 = Math.ceil(wxAt(box.width)) + 1;
  const y0 = Math.floor(wyAt(0)) - 1, y1 = Math.ceil(wyAt(box.height)) + 1;

  const g = ctx.createLinearGradient(0, 0, box.width, box.height);
  g.addColorStop(0, '#7a6349'); g.addColorStop(1, '#63513c');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, box.width, box.height);

  ctx.strokeStyle = 'rgba(0,0,0,.10)';
  ctx.lineWidth = Math.max(1, ps(1.5));
  for (let y = y0; y < y1; y += 1) {
    ctx.beginPath();
    ctx.moveTo(0, py(y)); ctx.lineTo(box.width, py(y)); ctx.stroke();
  }
  for (let y = y0; y < y1; y += 1) {
    for (let x = x0 + (Math.abs(y) % 2 ? 0 : 2); x < x1; x += 4) {
      ctx.beginPath();
      ctx.moveTo(px(x), py(y)); ctx.lineTo(px(x), py(y + 1)); ctx.stroke();
    }
  }
  // vignette, so the eye settles on the middle of the room
  const v = ctx.createRadialGradient(
    box.width / 2, box.height / 2, Math.min(box.width, box.height) * 0.35,
    box.width / 2, box.height / 2, Math.max(box.width, box.height) * 0.75);
  v.addColorStop(0, 'rgba(0,0,0,0)'); v.addColorStop(1, 'rgba(0,0,0,.40)');
  ctx.fillStyle = v;
  ctx.fillRect(0, 0, box.width, box.height);

  // rug under the meeting area
  roundRect(px(1.6), py(10.4), ps(6.6 * TILE), ps(4.2 * TILE), ps(14));
  ctx.fillStyle = 'rgba(90,70,58,.55)'; ctx.fill();
}

function drawWalls() {
  const box = V.box;
  ctx.fillStyle = '#2c241d';
  ctx.fillRect(0, 0, box.width, py(1.6));
  ctx.fillStyle = '#3a2f26';
  ctx.fillRect(0, py(1.45), box.width, ps(0.18 * TILE));

  // manager's glass partition
  ctx.strokeStyle = 'rgba(190,220,235,.30)';
  ctx.lineWidth = Math.max(2, ps(4));
  ctx.beginPath();
  ctx.moveTo(px(8.6), py(1.6)); ctx.lineTo(px(8.6), py(5.2));
  ctx.moveTo(px(8.6), py(7.2)); ctx.lineTo(px(8.6), py(8.8));
  ctx.lineTo(px(1.0), py(8.8));
  ctx.stroke();
  ctx.fillStyle = 'rgba(190,220,235,.05)';
  ctx.fillRect(px(1), py(1.6), ps(7.6 * TILE), ps(7.2 * TILE));

  label('MANAGER', 4.8, 2.15, 'rgba(255,255,255,.28)', 11);

  // whiteboard
  roundRect(px(12), py(0.35), ps(6 * TILE), ps(1 * TILE), ps(4));
  ctx.fillStyle = '#e8e3d8'; ctx.fill();
  ctx.strokeStyle = '#b9b1a3'; ctx.lineWidth = ps(1.5); ctx.stroke();
  const running = S.tasks.filter(t => t.status === 'running').length;
  const queued = S.tasks.filter(t => t.status === 'queued').length;
  const done = S.tasks.filter(t => t.status === 'done').length;
  label(`${queued} queued   ${running} active   ${done} done`, 15, 0.95, '#4a4136', 12, 'center');
}

function drawFurniture() {
  plant(9.9, 2.6); plant(25.0, 14.6); plant(1.4, 2.6);
  cooler(9.9, 13.2);

  // meeting table
  ctx.beginPath();
  ctx.ellipse(px(4.9), py(12.4), ps(2.1 * TILE), ps(1.3 * TILE), 0, 0, 6.284);
  ctx.fillStyle = '#8a6f52'; ctx.fill();
  ctx.strokeStyle = 'rgba(0,0,0,.22)'; ctx.lineWidth = ps(2); ctx.stroke();
  for (const [cx, cy] of [[2.4, 12.4], [7.4, 12.4], [4.9, 10.7], [4.9, 14.1]]) {
    roundRect(px(cx) - ps(11), py(cy) - ps(11), ps(22), ps(22), ps(5));
    ctx.fillStyle = '#4a3d31'; ctx.fill();
  }
}

function drawDesk(r) {
  const [x, y] = r.desk;
  // desk top
  roundRect(px(x) - ps(46), py(y) - ps(20), ps(92), ps(40), ps(6));
  ctx.fillStyle = '#8d7053'; ctx.fill();
  ctx.strokeStyle = 'rgba(0,0,0,.25)'; ctx.lineWidth = ps(1.5); ctx.stroke();

  // monitor
  const sp = V.sprites[r.id];
  const lit = sp && (sp.state === 'working' || sp.state === 'thinking');
  roundRect(px(x) - ps(20), py(y) - ps(28), ps(40), ps(26), ps(3));
  ctx.fillStyle = '#20262b'; ctx.fill();
  roundRect(px(x) - ps(17), py(y) - ps(25.5), ps(34), ps(21), ps(2));
  ctx.fillStyle = lit ? r.color : '#39424a';
  ctx.globalAlpha = lit ? 0.55 + 0.25 * Math.sin(V.t * 4) : 0.5;
  ctx.fill();
  ctx.globalAlpha = 1;

  // keyboard + mug
  roundRect(px(x) - ps(15), py(y) + ps(2), ps(30), ps(9), ps(2));
  ctx.fillStyle = '#2b3238'; ctx.fill();
  ctx.beginPath();
  ctx.arc(px(x) + ps(28), py(y) + ps(5), ps(5), 0, 6.284);
  ctx.fillStyle = '#c9c1b4'; ctx.fill();

  // nameplate
  label(`${r.emoji} ${r.name}`, x, y + 1.95, 'rgba(255,255,255,.72)', 11.5, 'center');
  label(r.title, x, y + 2.35, 'rgba(255,255,255,.38)', 10, 'center');
}

function drawAgent(r) {
  const sp = V.sprites[r.id];
  if (!sp) return;
  const x = px(sp.x), y = py(sp.y);
  const bob = Math.sin(sp.bob) * ps(sp.state === 'idle' ? 1.2 : 2);
  const selected = V.selected === r.id, hovered = V.hover === r.id;

  if (selected || hovered) {
    ctx.beginPath();
    ctx.ellipse(x, y + ps(6), ps(26), ps(11), 0, 0, 6.284);
    ctx.fillStyle = selected ? 'rgba(217,138,79,.30)' : 'rgba(255,255,255,.12)';
    ctx.fill();
  }

  ctx.beginPath();
  ctx.ellipse(x, y + ps(6), ps(15), ps(6), 0, 0, 6.284);
  ctx.fillStyle = 'rgba(0,0,0,.30)'; ctx.fill();

  // chair
  roundRect(x - ps(15), y - ps(6), ps(30), ps(16), ps(6));
  ctx.fillStyle = '#3b3129'; ctx.fill();

  // body
  roundRect(x - ps(12), y - ps(20) + bob, ps(24), ps(24), ps(9));
  ctx.fillStyle = r.color; ctx.fill();

  // arms reaching for the keyboard while working
  const reach = sp.state === 'working' ? ps(4) + Math.sin(V.t * 9) * ps(1.5) : 0;
  ctx.fillStyle = shade(r.color, -18);
  roundRect(x - ps(15), y - ps(14) + bob, ps(6), ps(13) + reach, ps(3)); ctx.fill();
  roundRect(x + ps(9), y - ps(14) + bob, ps(6), ps(13) + reach, ps(3)); ctx.fill();

  // head
  ctx.beginPath();
  ctx.arc(x, y - ps(26) + bob, ps(9), 0, 6.284);
  ctx.fillStyle = '#e8c9a8'; ctx.fill();
  // hair
  ctx.beginPath();
  ctx.arc(x, y - ps(28) + bob, ps(9), Math.PI, 0);
  ctx.fillStyle = shade(r.color, -55); ctx.fill();

  if (sp.state === 'blocked') statusMark(x, y - ps(44) + bob, '!', '#d9a441');
  if (sp.state === 'waiting') statusMark(x, y - ps(44) + bob, '⏳', '#8aa0b5');
}

function statusMark(x, y, glyph, color) {
  const pulse = 1 + 0.12 * Math.sin(V.t * 5);
  ctx.beginPath();
  ctx.arc(x, y, ps(11) * pulse, 0, 6.284);
  ctx.fillStyle = color; ctx.fill();
  ctx.fillStyle = '#1a1206';
  ctx.font = `700 ${ps(13)}px system-ui`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(glyph, x, y + ps(0.5));
}

function drawBubble(r) {
  const sp = V.sprites[r.id];
  if (!sp) return;
  const x = px(sp.x), y = py(sp.y) - ps(40);

  if (sp.bubble) {
    speech(x, y, sp.bubble.text, r.color);
  } else if (sp.state === 'thinking') {
    const n = 1 + Math.floor(V.t * 2.5) % 3;
    speech(x, y, '.'.repeat(n), r.color, true);
  } else if (sp.tool && V.t - sp.toolAt < 2.5) {
    speech(x, y, '⚙ ' + sp.tool, r.color, true);
  }
}

function speech(x, y, text, color, small = false) {
  const size = ps(small ? 12 : 12.5);
  ctx.font = `${small ? 500 : 400} ${size}px system-ui`;
  const maxW = ps(190);
  const lines = wrap(text, maxW, 4);
  const w = Math.max(ps(34), ...lines.map(l => ctx.measureText(l).width)) + ps(20);
  const h = lines.length * size * 1.35 + ps(14);
  const bx = x - w / 2, by = y - h;

  roundRect(bx, by, w, h, ps(9));
  ctx.fillStyle = 'rgba(20,17,14,.92)'; ctx.fill();
  ctx.strokeStyle = color; ctx.lineWidth = ps(1.5); ctx.globalAlpha = .6;
  ctx.stroke(); ctx.globalAlpha = 1;

  ctx.beginPath();
  ctx.moveTo(x - ps(6), by + h); ctx.lineTo(x, by + h + ps(8));
  ctx.lineTo(x + ps(6), by + h);
  ctx.fillStyle = 'rgba(20,17,14,.92)'; ctx.fill();

  ctx.fillStyle = '#ece3d6';
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  lines.forEach((l, i) => ctx.fillText(l, x, by + ps(7) + i * size * 1.35));
}

function wrap(text, maxW, maxLines) {
  const words = String(text).replace(/\s+/g, ' ').trim().split(' ');
  const lines = []; let line = '';
  for (const word of words) {
    const test = line ? line + ' ' + word : word;
    if (ctx.measureText(test).width > maxW && line) { lines.push(line); line = word; }
    else line = test;
    if (lines.length === maxLines) break;
  }
  if (lines.length < maxLines && line) lines.push(line);
  if (lines.length === maxLines) {
    let l = lines[maxLines - 1];
    while (ctx.measureText(l + '…').width > maxW && l.length > 4) l = l.slice(0, -1);
    lines[maxLines - 1] = l + '…';
  }
  return lines;
}

function flyer(from, to) {
  const a = S.byId[from], b = S.byId[to];
  if (!a || !b) return;
  V.flyers.push({
    x0: a.desk[0], y0: a.desk[1] + 0.8,
    x1: b.desk[0], y1: b.desk[1] + 0.8,
    p: 0, dur: 1.1, color: b.color
  });
}

function drawFlyers() {
  for (const f of V.flyers) {
    const t = f.p, ease = t * t * (3 - 2 * t);
    const x = px(f.x0 + (f.x1 - f.x0) * ease);
    const y = py(f.y0 + (f.y1 - f.y0) * ease) - ps(30) - Math.sin(t * Math.PI) * ps(34);
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(Math.sin(t * 6) * 0.25);
    roundRect(-ps(11), -ps(8), ps(22), ps(16), ps(2));
    ctx.fillStyle = '#f2ead9'; ctx.fill();
    ctx.strokeStyle = f.color; ctx.lineWidth = ps(1.5); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(-ps(11), -ps(8)); ctx.lineTo(0, ps(1)); ctx.lineTo(ps(11), -ps(8));
    ctx.stroke();
    ctx.restore();
  }
}

function plant(x, y) {
  roundRect(px(x) - ps(11), py(y), ps(22), ps(18), ps(4));
  ctx.fillStyle = '#8a5a3c'; ctx.fill();
  for (let i = 0; i < 6; i++) {
    const a = -Math.PI / 2 + (i - 2.5) * 0.42 + Math.sin(V.t * 0.7 + i) * 0.05;
    ctx.beginPath();
    ctx.ellipse(px(x) + Math.cos(a) * ps(11), py(y) + Math.sin(a) * ps(15),
      ps(6), ps(12), a + Math.PI / 2, 0, 6.284);
    ctx.fillStyle = i % 2 ? '#4e7a44' : '#3f6838'; ctx.fill();
  }
}

function cooler(x, y) {
  roundRect(px(x) - ps(10), py(y) - ps(4), ps(20), ps(30), ps(4));
  ctx.fillStyle = '#cfd8dc'; ctx.fill();
  roundRect(px(x) - ps(8), py(y) - ps(22), ps(16), ps(20), ps(6));
  ctx.fillStyle = 'rgba(120,190,220,.75)'; ctx.fill();
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) ctx.roundRect(x, y, w, h, r);
  else ctx.rect(x, y, w, h);
}

function label(text, wx, wy, color, size, align = 'left') {
  ctx.fillStyle = color;
  ctx.font = `500 ${ps(size)}px system-ui`;
  ctx.textAlign = align; ctx.textBaseline = 'middle';
  ctx.fillText(text, px(wx), py(wy));
}

function shade(hex, amt) {
  const n = parseInt(hex.slice(1), 16);
  const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255]
    .map(v => Math.max(0, Math.min(255, v + amt)));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

/* ------------------------------------------------------------------ UI -- */

function wireUI() {
  $('send').onclick = send;
  $('msg').addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
  $('approvalsBtn').onclick = () => $('modal').classList.remove('hidden');
  $('modalClose').onclick = () => $('modal').classList.add('hidden');
  $('modal').addEventListener('click', (e) => {
    if (e.target === $('modal')) $('modal').classList.add('hidden');
  });

  canvas.addEventListener('mousemove', (e) => {
    const hit = pick(e);
    V.hover = hit;
    canvas.style.cursor = hit ? 'pointer' : 'default';
    const tip = $('tip');
    if (hit) {
      const r = S.byId[hit], a = S.agents[hit] || {};
      tip.innerHTML = `<b>${r.emoji} ${r.name} — ${r.title}</b>` +
        `${a.status || 'idle'}${a.detail ? ' · ' + escapeHtml(a.detail) : ''}`;
      tip.style.left = Math.min(e.clientX + 14, innerWidth - 300) + 'px';
      tip.style.top = (e.clientY + 16) + 'px';
      tip.classList.remove('hidden');
    } else tip.classList.add('hidden');
  });
  canvas.addEventListener('mouseleave', () => {
    V.hover = null; $('tip').classList.add('hidden');
  });
  canvas.addEventListener('click', (e) => {
    const hit = pick(e);
    V.selected = hit === V.selected ? null : hit;
    if (V.selected) showAgent(V.selected); else renderFeed();
  });
}

function pick(e) {
  const box = canvas.getBoundingClientRect();
  const mx = e.clientX - box.left, my = e.clientY - box.top;
  for (const r of S.roster) {
    const sp = V.sprites[r.id];
    const dx = mx - px(sp.x), dy = my - (py(sp.y) - ps(14));
    if (dx * dx + dy * dy < ps(24) ** 2) return r.id;
  }
  return null;
}

async function send() {
  const input = $('msg');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  $('send').disabled = true;
  try {
    await fetch('/api/message', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });
  } finally { $('send').disabled = false; input.focus(); }
}

async function decide(id, approved, response) {
  await fetch('/api/approval', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, approved, response })
  });
  refreshApprovals();
}

/* -------------------------------------------------------------- panels -- */

function renderSpend() {
  const day = (S.spend.day || {}).usd ?? 0;
  const cap = S.spend.daily_budget || 0;
  $('spend').textContent = cap ? `$${day.toFixed(3)} / $${cap.toFixed(2)}` : `$${day.toFixed(3)}`;
}

function tickClock() {
  if (!S.startedAt) return;
  const s = Math.max(0, Math.floor(Date.now() / 1000 - S.startedAt));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  $('uptime').textContent = h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}

function renderBoard() {
  const groups = {
    'In progress': S.tasks.filter(t => t.status === 'running'),
    'Queued': S.tasks.filter(t => t.status === 'queued'),
    'Finished': S.tasks.filter(t => ['done', 'failed'].includes(t.status)).slice(0, 12)
  };
  const board = $('board');
  board.innerHTML = '';
  for (const [name, items] of Object.entries(groups)) {
    if (!items.length) continue;
    const h = document.createElement('h3');
    h.textContent = `${name} (${items.length})`;
    board.appendChild(h);
    for (const t of items) {
      const who = S.byId[t.assignee] || {};
      const card = document.createElement('div');
      card.className = 'card ' + t.status;
      card.style.borderLeftColor = who.color || 'var(--accent)';
      card.innerHTML = `<div>${escapeHtml(t.title)}</div>
        <div class="who">${who.emoji || ''} ${escapeHtml(who.name || t.assignee)}</div>`;
      card.title = t.result || t.brief || '';
      board.appendChild(card);
    }
  }
  if (!board.children.length) {
    board.innerHTML = '<p class="muted">Nothing on the board yet.</p>';
  }
}

function renderApprovals() {
  const list = S.approvals || [];
  $('approvalsCount').textContent = list.length;
  $('approvalsBtn').classList.toggle('hidden', !list.length);
  if (!list.length) $('modal').classList.add('hidden');

  const box = $('approvals');
  box.innerHTML = '';
  for (const a of list) {
    const who = S.byId[a.agent_id] || {};
    const el = document.createElement('div');
    el.className = 'approval';
    const isQuestion = a.kind === 'question';
    el.innerHTML = `
      <div class="from">${who.emoji || ''} <b>${escapeHtml(who.name || a.agent_id)}</b>
        ${isQuestion ? 'is asking you' : 'wants to run'}${a.detail ? ' · ' + escapeHtml(a.detail) : ''}</div>
      <code>${escapeHtml(a.action)}</code>
      ${isQuestion ? '<input placeholder="Your answer…">' : ''}
      <div class="actions"></div>`;
    const actions = el.querySelector('.actions');
    const input = el.querySelector('input');

    const yes = document.createElement('button');
    yes.className = 'btn-ok';
    yes.textContent = isQuestion ? 'Reply' : 'Approve';
    yes.onclick = () => decide(a.id, true, input ? input.value : '');
    const no = document.createElement('button');
    no.className = 'btn-no';
    no.textContent = isQuestion ? 'Skip' : 'Deny';
    no.onclick = () => decide(a.id, false, '');
    actions.append(yes, no);
    if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter') yes.click(); });
    box.appendChild(el);
  }
  if (list.length) $('modal').classList.remove('hidden');
}

function pushFeed(ev, quiet) {
  V.feed.push(ev);
  if (V.feed.length > 300) V.feed.shift();
  if (!quiet) {
    if (V.selected && ev.agent_id === V.selected) appendLine($('feed'), ev);
    else if (!V.selected) appendLine($('feed'), ev);
  }
}

function renderFeed() {
  $('inspectorHead').innerHTML =
    '<h2>Office feed</h2><p class="muted">Click anyone on the floor to watch them work.</p>';
  const feed = $('feed');
  feed.innerHTML = '';
  for (const ev of V.feed.slice(-120)) appendLine(feed, ev);
}

async function showAgent(id) {
  const r = S.byId[id], a = S.agents[id] || {};
  $('inspectorHead').innerHTML = `
    <div class="agentCard">
      <div class="name">${r.emoji} ${escapeHtml(r.name)}</div>
      <div class="role">${escapeHtml(r.title)}</div>
      <span class="badge">${escapeHtml(a.status || 'idle')}</span>
      ${a.detail ? `<span class="badge">${escapeHtml(a.detail)}</span>` : ''}
    </div>`;
  const feed = $('feed');
  feed.innerHTML = '<p class="muted">loading…</p>';
  const data = await (await fetch('/api/agent/' + encodeURIComponent(id))).json();
  feed.innerHTML = '';
  if (!data.transcript.length) {
    feed.innerHTML = '<p class="muted">Nothing on the record yet.</p>';
  }
  for (const row of data.transcript) {
    appendLine(feed, {
      type: 'agent.' + row.kind, agent_id: id, ts: row.ts,
      payload: { text: row.body }
    });
  }
}

function appendLine(feed, ev) {
  const kind = ev.type.split('.').pop();
  const who = ev.agent_id === 'user' ? { name: 'You', emoji: '🧑' }
    : (S.byId[ev.agent_id] || { name: ev.agent_id || 'office', emoji: '' });
  const p = ev.payload || {};

  let text, cls = kind;
  if (kind === 'tool') { text = `${p.tool}${p.args ? ' · ' + p.args : ''}`; }
  else if (kind === 'created') { text = `new task: ${p.title} → ${p.assignee}`; cls = 'tool'; }
  else if (kind === 'updated') { text = `task ${p.status}`; cls = 'tool'; }
  else if (kind === 'requested') { text = `needs approval: ${p.action}`; cls = 'error'; }
  else if (kind === 'decided') { text = `approval ${p.status}`; cls = 'tool'; }
  else text = p.text || '';
  if (!text) return;

  const el = document.createElement('div');
  el.className = 'line ' + (ev.agent_id === 'user' ? 'user' : cls);
  el.innerHTML =
    `<span class="when">${new Date((ev.ts || Date.now() / 1000) * 1000)
      .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>` +
    `<span class="who" style="color:${(S.byId[ev.agent_id] || {}).color || ''}">` +
    `${who.emoji} ${escapeHtml(who.name)}</span> ${escapeHtml(text)}`;

  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  feed.appendChild(el);
  while (feed.children.length > 200) feed.removeChild(feed.firstChild);
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

boot();
