/* Digital Office - front end.
 *
 * A pure viewer. All state lives in the daemon's SQLite file; this reloads a
 * snapshot on connect and then follows an SSE event stream, so closing the tab
 * costs nothing and reopening it replays the office exactly as it stands.
 *
 * Rendering is pixel art: the world is drawn once at 640x416 logical pixels on
 * an offscreen canvas, then blitted at an integer device-pixel scale with
 * smoothing off, so every pixel stays square. Text and bubbles are drawn on the
 * main canvas at native resolution, because pixel-font body copy is charming
 * for about four seconds and unreadable thereafter.
 */
'use strict';

/* ------------------------------------------------------------------ map -- */

// The world is kept deliberately compact in tiles. Rendering uses an integer
// device-pixel scale to keep pixels square, so a floor plan a few tiles too
// wide drops the whole office from 2x to 1x and wastes half the pane.
const T = 16, W = 29, H = 22;          // logical tiles
const LOGW = W * T, LOGH = H * T;      // 464 x 352

// Floor plan. Desk coordinates must match ROSTER in office/config.py.
//
//   +----------+--------------------------+
//   | MANAGER  |                          |
//   |     door>|  OPEN PLAN  [desks row 1]|
//   +----------+                          |
//   | lobby    <  (open corridor)         |
//   +--^door---+             [desks row 2]|
//   | BREAK    |                          |
//   +----------+--------------------------+

const OPEN_PLAN_X = 10;                // first column of the open-plan side
const MGR_DOOR = { x: 10, y: 5 };
const BREAK_DOOR = { x: 5, y: 13 };
const SCOLD_SPOT = { x: 5, y: 7 };     // where you stand to be told off
const BREAK_SPOTS = [{ x: 2, y: 17 }, { x: 4, y: 18 }, { x: 6, y: 19 },
                     { x: 5, y: 16 }, { x: 7, y: 15 }, { x: 3, y: 16 }];

const FURNITURE = [
  // break room
  { kind: 'coffee', tiles: [[1, 15], [2, 15]] },
  { kind: 'sofa', tiles: [[1, 19], [2, 19], [3, 19]] },
  { kind: 'table', tiles: [[6, 17], [7, 17]] },
  { kind: 'vending', tiles: [[8, 20]] },
  { kind: 'plant', tiles: [[8, 15]] },
  // manager office
  { kind: 'plant', tiles: [[1, 1]] },
  { kind: 'cabinet', tiles: [[8, 1], [8, 2]] },
  // open plan
  { kind: 'cooler', tiles: [[12, 2]] },
  { kind: 'printer', tiles: [[26, 10]] },
  { kind: 'plant', tiles: [[26, 20]] },
  { kind: 'plant', tiles: [[11, 20]] },
];

let SOLID = null;

function buildMap() {
  SOLID = new Uint8Array(W * H);
  const set = (x, y) => {
    if (x >= 0 && x < W && y >= 0 && y < H) SOLID[y * W + x] = 1;
  };
  for (let x = 0; x < W; x++) { set(x, 0); set(x, H - 1); }
  for (let y = 0; y < H; y++) { set(0, y); set(W - 1, y); }

  for (let y = 1; y <= 9; y++) if (y !== MGR_DOOR.y) set(OPEN_PLAN_X, y);  // glass
  for (let x = 1; x <= OPEN_PLAN_X; x++) set(x, 9);                        // office back
  for (let x = 1; x <= OPEN_PLAN_X; x++) if (x !== BREAK_DOOR.x) set(x, 13);
  for (let y = 13; y <= H - 2; y++) set(OPEN_PLAN_X, y);                   // break wall

  for (const f of FURNITURE) for (const [x, y] of f.tiles) set(x, y);
  for (const r of S.roster) {
    const [x, y] = r.desk;
    set(x - 1, y); set(x, y); set(x + 1, y);
  }
}

const walkable = (x, y) =>
  x >= 0 && x < W && y >= 0 && y < H && !SOLID[y * W + x];

/** Breadth-first path between tiles. Small grid, runs in microseconds. */
function findPath(sx, sy, gx, gy) {
  sx = Math.round(sx); sy = Math.round(sy);
  gx = Math.round(gx); gy = Math.round(gy);
  if (!walkable(gx, gy)) return [];
  if (sx === gx && sy === gy) return [];

  const prev = new Int32Array(W * H).fill(-1);
  const seen = new Uint8Array(W * H);
  const queue = [sy * W + sx];
  seen[sy * W + sx] = 1;
  const goal = gy * W + gx;

  for (let head = 0; head < queue.length; head++) {
    const cur = queue[head];
    if (cur === goal) break;
    const cx = cur % W, cy = (cur / W) | 0;
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const nx = cx + dx, ny = cy + dy;
      if (!walkable(nx, ny)) continue;
      const n = ny * W + nx;
      if (seen[n]) continue;
      seen[n] = 1; prev[n] = cur; queue.push(n);
    }
  }
  if (!seen[goal]) return [];

  const path = [];
  for (let c = goal; c !== -1 && c !== sy * W + sx; c = prev[c]) {
    path.push({ x: c % W, y: (c / W) | 0 });
  }
  return path.reverse();
}

/* ---------------------------------------------------------------- state -- */

const S = {
  roster: [], byId: {}, agents: {}, tasks: [], approvals: [],
  spend: {}, backend: '', startedAt: 0, seq: 0
};
const V = { sprites: {}, feed: [], selected: null, hover: null, t: 0, scale: 1 };

const $ = (id) => document.getElementById(id);
const canvas = $('floor');
const ctx = canvas.getContext('2d');

// Offscreen world buffer, drawn at logical resolution.
const world = document.createElement('canvas');
world.width = LOGW; world.height = LOGH;
const g = world.getContext('2d');

/* ----------------------------------------------------------------- boot -- */

async function boot() {
  await loadState();
  buildMap();
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
  S.startedAt = state.started_at;
  S.seq = state.seq;

  for (const r of S.roster) {
    S.agents[r.id] = { status: 'idle', detail: '' };
    V.sprites[r.id] = {
      id: r.id,
      x: r.desk[0], y: r.desk[1] + 1,        // the seat, one tile below the desk
      home: { x: r.desk[0], y: r.desk[1] + 1 },
      path: [], script: [], wait: 0, walked: 0,
      dir: 'up', state: 'idle', activity: 'desk',
      bubble: null, thought: '', tool: '', toolAt: -99
    };
  }
  for (const a of state.agents) {
    if (S.agents[a.id]) { S.agents[a.id] = a; V.sprites[a.id].state = a.status; }
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
  const p = ev.payload || {};

  switch (ev.type) {
    case 'agent.status':
      if (S.agents[ev.agent_id]) {
        S.agents[ev.agent_id].status = p.status;
        S.agents[ev.agent_id].detail = p.detail || '';
      }
      if (sp) {
        sp.state = p.status;
        // Work arrives - stop whatever social thing you were doing.
        if (p.status === 'working' && sp.activity !== 'desk' &&
            sp.activity !== 'scolded') sendHome(sp);
      }
      break;

    case 'agent.say':
    case 'agent.message_user':
      say(ev.agent_id, p.text); pushFeed(ev); break;
    case 'agent.thinking':
      if (sp) { sp.thought = p.text; sp.state = 'thinking'; }
      pushFeed(ev); break;
    case 'agent.tool':
      if (sp) { sp.tool = p.tool; sp.toolAt = V.t; }
      pushFeed(ev); break;
    case 'agent.error': pushFeed(ev); break;

    case 'handoff': flyer(ev.agent_id, p.to); break;

    case 'task.created':
    case 'task.updated': refreshTasks(); pushFeed(ev); break;
    case 'approval.requested':
    case 'approval.decided': refreshApprovals(); pushFeed(ev); break;
    case 'usage':
      if (p.spend) { S.spend.total = p.spend; renderSpend(); } break;
    case 'user.message': pushFeed(ev); break;
    case 'office.budget':
      pushFeed({ ...ev, type: 'agent.error', payload: { text: p.reason } }); break;

    /* ---- the social life of the office ---- */
    case 'social.break': {
      // Belt and braces: the daemon already excludes anyone mid-telling-off,
      // but a stale event must never pull them out of the manager's office.
      if (!sp || sp.activity === 'scolded') break;
      const spot = BREAK_SPOTS[(Math.random() * BREAK_SPOTS.length) | 0];
      sp.activity = 'break';
      say(ev.agent_id, p.line);
      sp.script = [{ type: 'goto', x: spot.x, y: spot.y }, { type: 'face', dir: 'down' }];
      pushFeed(ev); break;
    }
    case 'social.return':
      if (sp) { say(ev.agent_id, p.line); sendHome(sp); }
      pushFeed(ev); break;

    case 'social.summoned':
      if (!sp) break;
      sp.activity = 'scolded';
      say(ev.agent_id, '...');
      sp.script = [{ type: 'goto', x: SCOLD_SPOT.x, y: SCOLD_SPOT.y },
                   { type: 'face', dir: 'up' }];
      say('manager', 'Have you got a minute?');
      V.sprites.manager.dir = 'down';
      pushFeed(ev); break;

    case 'social.scold':
      say('manager', p.line);
      V.sprites.manager.dir = 'down';
      setTimeout(() => say(ev.agent_id, p.reply), 2600);
      pushFeed(ev); break;

    case 'social.dismissed':
      if (sp) sendHome(sp);
      V.sprites.manager.dir = 'up';
      pushFeed(ev); break;

    case 'social.patrol': {
      const mgr = V.sprites.manager;
      if (!mgr) break;
      mgr.activity = 'patrol';
      mgr.script = [];
      for (const stop of p.route || []) {
        const target = S.byId[stop.agent];
        if (!target) continue;
        mgr.script.push(
          { type: 'goto', x: target.desk[0], y: target.desk[1] + 2 },
          { type: 'face', dir: 'up' },
          { type: 'say', text: stop.line },
          { type: 'wait', s: 1.4 },
          { type: 'sayOther', who: stop.agent, text: stop.reply },
          { type: 'wait', s: 2.2 });
      }
      mgr.script.push({ type: 'goto', x: mgr.home.x, y: mgr.home.y },
                      { type: 'face', dir: 'up' },
                      { type: 'done' });
      pushFeed(ev); break;
    }
  }
}

function say(agentId, text) {
  const sp = V.sprites[agentId];
  if (!sp || !text) return;
  sp.bubble = { text, until: V.t + Math.min(14, 3.2 + String(text).length / 22) };
}

function sendHome(sp) {
  sp.activity = 'desk';
  sp.script = [{ type: 'goto', x: sp.home.x, y: sp.home.y },
               { type: 'face', dir: 'up' }, { type: 'done' }];
}

async function refreshTasks() {
  S.tasks = (await (await fetch('/api/tasks')).json()).tasks;
  renderBoard();
}
async function refreshApprovals() {
  const state = await (await fetch('/api/state')).json();
  S.approvals = state.approvals;
  renderApprovals();
}

/* ------------------------------------------------------------ simulation -- */

const SPEED = 4.2;   // tiles per second

function update(dt, stalled) {
  for (const id in V.sprites) {
    const sp = V.sprites[id];
    if (sp.bubble && V.t > sp.bubble.until) sp.bubble = null;
    stepScript(sp);
    if (stalled) snapToGoal(sp); else move(sp, dt);
  }
}

/** Browsers throttle requestAnimationFrame in a hidden tab, so a viewer on a
 *  second monitor would come back to agents crawling several minutes behind
 *  the story the daemon has already told. After a stall, put everyone where
 *  they were headed rather than animating the backlog. */
function snapToGoal(sp) {
  if (!sp.path.length) return;
  const end = sp.path[sp.path.length - 1];
  sp.x = end.x; sp.y = end.y;
  sp.path.length = 0;
  sp.wait = 0;
}

function stepScript(sp) {
  if (sp.wait > 0) return;
  while (sp.script.length) {
    const step = sp.script[0];
    if (step.type === 'goto') {
      if (!step.started) {
        step.started = true;
        sp.path = findPath(sp.x, sp.y, step.x, step.y);
        if (!sp.path.length) { sp.script.shift(); continue; }
        return;
      }
      if (sp.path.length) return;
      sp.script.shift(); continue;
    }
    if (step.type === 'face') { sp.dir = step.dir; sp.script.shift(); continue; }
    if (step.type === 'say') { say(sp.id, step.text); sp.script.shift(); continue; }
    if (step.type === 'sayOther') { say(step.who, step.text); sp.script.shift(); continue; }
    if (step.type === 'wait') { sp.wait = step.s; sp.script.shift(); return; }
    if (step.type === 'done') { sp.activity = 'desk'; sp.script.shift(); continue; }
    sp.script.shift();
  }
}

function move(sp, dt) {
  if (sp.wait > 0) sp.wait -= dt;
  if (!sp.path.length) return;
  const next = sp.path[0];
  const dx = next.x - sp.x, dy = next.y - sp.y;
  const dist = Math.hypot(dx, dy);
  const step = SPEED * dt;
  if (dist <= step) {
    sp.x = next.x; sp.y = next.y; sp.path.shift();
  } else {
    sp.x += dx / dist * step; sp.y += dy / dist * step;
    sp.dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left')
                                         : (dy > 0 ? 'down' : 'up');
  }
  sp.walked += step;
}

const seated = (sp) =>
  !sp.path.length && Math.abs(sp.x - sp.home.x) < 0.1 && Math.abs(sp.y - sp.home.y) < 0.1;

/* -------------------------------------------------------------- painting -- */

let last = 0;
function frame(ts) {
  const raw = (ts - last) / 1000 || 0;
  const stalled = raw > 0.5;              // tab was hidden or the thread blocked
  const dt = Math.min(0.05, raw);
  last = ts; V.t += dt;
  fit();
  update(dt, stalled);
  paintWorld();
  blit();
  paintOverlay();
  requestAnimationFrame(frame);
}

function fit() {
  const box = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(box.width * dpr));
  canvas.height = Math.max(1, Math.round(box.height * dpr));

  // Integer DEVICE pixels per logical pixel keeps every pixel square. The small
  // bias lets us step up a level and crop a sliver of border wall rather than
  // leaving a third of the pane empty.
  const raw = Math.min(canvas.width / LOGW, canvas.height / LOGH);
  V.scale = Math.max(1, Math.floor(raw + 0.18));
  V.dpr = dpr; V.box = box;
  V.dw = LOGW * V.scale; V.dh = LOGH * V.scale;
  V.ox = Math.round((canvas.width - V.dw) / 2);
  V.oy = Math.round((canvas.height - V.dh) / 2);
  V.k = T * V.scale / dpr;                       // CSS px per world tile
  V.cx = V.ox / dpr; V.cy = V.oy / dpr;          // blit origin in CSS px
}

const X = (wx) => V.cx + wx * V.k;               // world tile -> CSS px
const Y = (wy) => V.cy + wy * V.k;

function blit() {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = '#1b1a24';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(world, 0, 0, LOGW, LOGH, V.ox, V.oy, V.dw, V.dh);
  ctx.setTransform(V.dpr, 0, 0, V.dpr, 0, 0);
}

/* ---- world buffer (pixel art) ---- */

function paintWorld() {
  g.clearRect(0, 0, LOGW, LOGH);
  paintFloors();
  paintWalls();
  for (const f of FURNITURE) paintFurniture(f);

  // Painter's algorithm: things lower on the screen occlude things above.
  const drawables = [];
  for (const r of S.roster) drawables.push({ y: r.desk[1], draw: () => paintDesk(r) });
  for (const r of S.roster) {
    const sp = V.sprites[r.id];
    drawables.push({ y: sp.y + 0.5, draw: () => paintPerson(r, sp) });
  }
  drawables.sort((a, b) => a.y - b.y).forEach(d => d.draw());
}

function px(x, y, w, h, color) { g.fillStyle = color; g.fillRect(x, y, w, h); }
function tile(tx, ty, color) { px(tx * T, ty * T, T, T, color); }

function paintFloors() {
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const checker = (x + y) % 2 === 0;
      const left = x < OPEN_PLAN_X;
      let color;
      if (left && y < 9) color = checker ? '#9c7c52' : '#94744c';        // manager: wood
      else if (left && y > 13) color = checker ? '#7e8f8c' : '#8b9c99';  // break: tile
      else if (left) color = checker ? '#6f6a63' : '#75706a';            // lobby
      else color = checker ? '#6b6660' : '#716c66';                      // open-plan carpet
      tile(x, y, color);
    }
  }
  // carpet speckle - a little noise stops the grid reading as graph paper
  g.fillStyle = 'rgba(0,0,0,.09)';
  for (let i = 0; i < 700; i++) {
    const x = (i * 79 + 13) % LOGW, y = (i * 137 + 41) % LOGH;
    if (x > OPEN_PLAN_X * T) g.fillRect(x, y, 1, 1);
  }
  // rug in the break room
  px(1 * T, 16 * T, 7 * T, 4 * T, 'rgba(150,90,70,.28)');
}

function paintWalls() {
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      if (!SOLID[y * W + x]) continue;
      if (isFurniture(x, y)) continue;
      const glass = x === OPEN_PLAN_X && y <= 9;
      if (glass) {
        px(x * T, y * T, T, T, 'rgba(150,200,215,.16)');
        px(x * T + 6, y * T, 4, T, 'rgba(190,225,240,.35)');
        px(x * T, y * T, T, 2, '#5b6d75');
        continue;
      }
      px(x * T, y * T, T, T, '#3c3a48');
      px(x * T, y * T, T, 3, '#4b4959');                 // lit top edge
      if (!SOLID[(y + 1) * W + x] || y === H - 1) {
        px(x * T, y * T + T - 4, T, 4, '#2a2833');       // shadowed face
      }
    }
  }
  doorway(MGR_DOOR.x, MGR_DOOR.y, 'v');
  doorway(BREAK_DOOR.x, BREAK_DOOR.y, 'h');

  roomSign(2, 0, 'MANAGER');
  roomSign(2, 13, 'BREAK ROOM');
}

function isFurniture(x, y) {
  for (const f of FURNITURE) for (const [fx, fy] of f.tiles)
    if (fx === x && fy === y) return true;
  for (const r of S.roster) {
    const [dx, dy] = r.desk;
    if (dy === y && Math.abs(dx - x) <= 1) return true;
  }
  return false;
}

function doorway(x, y, dir) {
  if (dir === 'v') {
    px(x * T, y * T, 3, T, '#6b5330'); px(x * T + T - 3, y * T, 3, T, '#6b5330');
  } else {
    px(x * T, y * T, T, 3, '#6b5330'); px(x * T, y * T + T - 3, T, 3, '#6b5330');
  }
}

function roomSign(tx, ty, text) {
  g.fillStyle = 'rgba(255,255,255,.34)';
  g.font = '7px monospace';
  g.textAlign = 'left'; g.textBaseline = 'top';
  g.fillText(text, tx * T, ty * T + 4);
}

function paintDesk(r) {
  const [tx, ty] = r.desk;
  const x = (tx - 1) * T, y = ty * T;
  px(x, y + 2, T * 3, T - 4, '#6b4f34');          // desk body
  px(x, y + 2, T * 3, 3, '#8a6a47');              // lit top
  px(x, y + T - 4, T * 3, 2, '#4a3524');          // shadow lip

  const sp = V.sprites[r.id];
  const lit = sp.state === 'working' || sp.state === 'thinking';
  // monitor
  px(x + 18, y - 8, 12, 11, '#23222c');
  px(x + 19, y - 7, 10, 8, lit ? r.color : '#3d4450');
  if (lit && Math.floor(V.t * 3) % 2) px(x + 20, y - 6, 6, 2, 'rgba(255,255,255,.55)');
  px(x + 22, y + 3, 4, 2, '#23222c');
  // keyboard + mug
  px(x + 16, y + 7, 14, 3, '#2c2b36');
  px(x + 36, y + 5, 5, 5, '#c9c1b4');
  px(x + 6, y + 5, 6, 4, '#d8d2c4');              // paper
}

/* Little person, 8x17 logical px, feet at (0,0) of the given point. */
function paintPerson(r, sp) {
  const cx = Math.round(sp.x * T + T / 2);
  const cy = Math.round(sp.y * T + T - 2);
  const sitting = seated(sp) && sp.activity === 'desk';
  const walking = sp.path.length > 0;
  const step = walking ? Math.floor(sp.walked * 4) % 4 : 0;
  const bob = sitting ? 0 : (step === 1 || step === 3 ? -1 : 0);
  const skin = '#e0b088', hair = shade(r.color, -70);
  const y0 = cy - (sitting ? 2 : 0) + bob;
  const P = (dx, dy, w, h, c) => px(cx + dx, y0 + dy, w, h, c);

  P(-5, -1, 10, 2, 'rgba(0,0,0,.28)');                    // shadow

  if (sitting) {
    P(-6, -9, 12, 9, shade(r.color, -45));                // chair back
  } else {
    const swing = step === 1 ? 1 : step === 3 ? -1 : 0;
    P(-3, -5 + Math.max(0, swing), 3, 5, '#3a3550');      // legs
    P(1, -5 + Math.max(0, -swing), 3, 5, '#3a3550');
  }

  P(-4, -12, 8, 8, r.color);                              // torso
  P(-4, -12, 8, 2, shade(r.color, 22));
  if (sp.dir !== 'up') P(-6, -11, 2, 6, shade(r.color, -20));   // arms
  P(4, -11, 2, 6, shade(r.color, -20));

  P(-4, -20, 8, 8, skin);                                 // head
  P(-4, -21, 8, 4, hair);                                 // hair
  if (sp.dir === 'up') { P(-4, -20, 8, 6, hair); }
  else if (sp.dir === 'left') { P(-4, -20, 3, 5, hair); P(-1, -16, 1, 1, '#2a2233'); }
  else if (sp.dir === 'right') { P(1, -20, 3, 5, hair); P(0, -16, 1, 1, '#2a2233'); }
  else { P(-2, -16, 1, 1, '#2a2233'); P(1, -16, 1, 1, '#2a2233'); }

  if (sitting && sp.state === 'working') {                // hands at the keyboard
    const t = Math.floor(V.t * 8) % 2;
    P(-3, -5 + t, 2, 2, skin); P(1, -5 + (1 - t), 2, 2, skin);
  }
  if (sp.activity === 'break') P(5, -10, 3, 4, '#f0ece2');  // coffee cup
}

function paintFurniture(f) {
  const [x0, y0] = f.tiles[0];
  const x = x0 * T, y = y0 * T;
  switch (f.kind) {
    case 'coffee':
      px(x, y + 2, T * 2, T - 2, '#4b4a56'); px(x + 3, y + 5, 8, 6, '#22212a');
      px(x + 4, y + 6, 6, 4, '#8b5a2b'); px(x + T + 3, y + 5, 9, 3, '#6f6e7c');
      px(x + T + 5, y + 10, 5, 5, '#d9d5cb'); break;
    case 'sofa':
      px(x, y + 3, T * 3, T - 3, '#7a4f52'); px(x, y, T * 3, 5, '#8e5f62');
      px(x + 2, y + 6, 12, 7, '#8e5f62'); px(x + T + 2, y + 6, 12, 7, '#8e5f62');
      px(x + T * 2 + 2, y + 6, 12, 7, '#8e5f62'); break;
    case 'table':
      px(x, y + 4, T * 2, T - 6, '#6b4f34'); px(x, y + 4, T * 2, 3, '#8a6a47');
      px(x + 6, y + 7, 6, 4, '#cfcabb'); break;
    case 'vending':
      px(x, y - T + 2, T, T * 2 - 2, '#3c5a54'); px(x + 2, y - T + 5, T - 5, T, '#7fc2b2');
      px(x + 3, y - T + 7, 3, 3, '#e8e3d8'); px(x + 8, y - T + 7, 3, 3, '#e8b34f'); break;
    case 'plant':
      px(x + 4, y + 8, 8, 7, '#8a5a3c'); px(x + 5, y + 6, 6, 3, '#6f4830');
      px(x + 5, y - 2, 6, 9, '#3f6838'); px(x + 2, y + 1, 4, 6, '#4e7a44');
      px(x + 10, y + 1, 4, 6, '#4e7a44'); break;
    case 'cooler':
      px(x + 3, y + 4, 10, 11, '#cfd8dc'); px(x + 4, y - 4, 8, 9, '#79b9d6');
      px(x + 5, y + 8, 6, 2, '#8b98a0'); break;
    case 'printer':
      px(x + 1, y + 4, 14, 10, '#4a4956'); px(x + 3, y + 2, 10, 4, '#5d5c6b');
      px(x + 3, y + 9, 10, 3, '#e8e3d8'); break;
    case 'cabinet':
      px(x + 1, y - T + 2, 14, T * 2 - 3, '#5a5462');
      px(x + 3, y - T + 6, 10, 2, '#847d90'); px(x + 3, y + 4, 10, 2, '#847d90'); break;
  }
}

/* ---- overlay (native resolution) ---- */

function paintOverlay() {
  // Name only - four titles side by side at this scale become one long smear.
  // The role is on the tooltip and in the inspector.
  for (const r of S.roster) {
    const sp = V.sprites[r.id];
    if (!seated(sp)) continue;
    plate(`${r.emoji} ${r.name}`, X(r.desk[0] + 0.5), Y(r.desk[1] + 2.15),
          'rgba(255,255,255,.82)', 11);
  }

  for (const r of S.roster) {
    const sp = V.sprites[r.id];
    const cx = X(sp.x + 0.5), cy = Y(sp.y + 1);
    if (V.selected === r.id || V.hover === r.id) {
      ctx.beginPath();
      ctx.ellipse(cx, cy - 2, V.k * 0.55, V.k * 0.26, 0, 0, 6.284);
      ctx.strokeStyle = V.selected === r.id ? '#e8a35f' : 'rgba(255,255,255,.5)';
      ctx.lineWidth = 2; ctx.stroke();
    }
    if (sp.state === 'blocked') mark(cx, cy - V.k * 2.3, '!', '#d9a441');
    else if (sp.state === 'waiting') mark(cx, cy - V.k * 2.3, '~', '#8aa0b5');
  }

  for (const r of S.roster) {
    const sp = V.sprites[r.id];
    const cx = X(sp.x + 0.5), top = Y(sp.y) - 12;
    if (sp.bubble) speech(cx, top, sp.bubble.text, r.color);
    else if (sp.state === 'thinking' && seated(sp))
      speech(cx, top, '.'.repeat(1 + Math.floor(V.t * 2.5) % 3), r.color);
    else if (sp.tool && V.t - sp.toolAt < 2.5)
      speech(cx, top, '⚙ ' + sp.tool, r.color);
  }
  drawFlyers();
}

function plate(text, cx, cy, color, size) {
  ctx.font = `600 ${size}px ui-monospace, Menlo, monospace`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillStyle = 'rgba(0,0,0,.55)';
  const w = ctx.measureText(text).width + 8;
  ctx.fillRect(cx - w / 2, cy - size * 0.7, w, size * 1.4);
  ctx.fillStyle = color;
  ctx.fillText(text, cx, cy);
}

function mark(x, y, glyph, color) {
  const pulse = 1 + 0.14 * Math.sin(V.t * 6);
  ctx.fillStyle = color;
  ctx.fillRect(x - 8 * pulse, y - 8 * pulse, 16 * pulse, 16 * pulse);
  ctx.fillStyle = '#1a1206';
  ctx.font = '700 13px system-ui';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(glyph, x, y + 1);
}

function speech(x, y, text, color) {
  ctx.font = '12.5px system-ui';
  const lines = wrap(text, 190, 4);
  const lh = 16;
  const w = Math.max(34, ...lines.map(l => ctx.measureText(l).width)) + 18;
  const h = lines.length * lh + 12;
  const bx = Math.round(x - w / 2), by = Math.round(y - h);

  ctx.fillStyle = 'rgba(16,15,22,.94)';
  ctx.fillRect(bx, by, w, h);
  ctx.fillStyle = color;
  ctx.fillRect(bx, by, w, 2);
  ctx.fillRect(Math.round(x) - 4, by + h, 8, 5);

  ctx.fillStyle = '#ece3d6';
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  lines.forEach((l, i) => ctx.fillText(l, x, by + 6 + i * lh));
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

const flyers = [];
function flyer(from, to) {
  const a = S.byId[from], b = S.byId[to];
  if (a && b) flyers.push({ a, b, p: 0 });
}
function drawFlyers() {
  for (let i = flyers.length - 1; i >= 0; i--) {
    const f = flyers[i];
    f.p += 1 / 70;
    if (f.p >= 1) { flyers.splice(i, 1); continue; }
    const e = f.p * f.p * (3 - 2 * f.p);
    const x = X(f.a.desk[0] + 0.5 + (f.b.desk[0] - f.a.desk[0]) * e);
    const y = Y(f.a.desk[1] + 0.5 + (f.b.desk[1] - f.a.desk[1]) * e)
      - Math.sin(f.p * Math.PI) * V.k * 1.6;
    ctx.fillStyle = '#f2ead9'; ctx.fillRect(x - 7, y - 5, 14, 10);
    ctx.fillStyle = f.b.color; ctx.fillRect(x - 7, y - 5, 14, 2);
  }
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
      const r = S.byId[hit], a = S.agents[hit] || {}, sp = V.sprites[hit];
      const where = sp.activity === 'break' ? ' · on a break'
        : sp.activity === 'scolded' ? " · in the manager's office"
        : sp.activity === 'patrol' ? ' · walking the floor' : '';
      tip.innerHTML = `<b>${r.emoji} ${r.name} — ${r.title}</b>` +
        `${a.status || 'idle'}${where}${a.detail ? ' · ' + escapeHtml(a.detail) : ''}`;
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
    const cx = X(sp.x + 0.5), cy = Y(sp.y + 0.4);
    if (Math.abs(mx - cx) < V.k * 0.6 && Math.abs(my - cy) < V.k) return r.id;
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
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });
  } finally { $('send').disabled = false; input.focus(); }
}

async function decide(id, approved, response) {
  await fetch('/api/approval', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, approved, response })
  });
  refreshApprovals();
}

/* -------------------------------------------------------------- panels -- */

function renderSpend() {
  const day = (S.spend.day || {}).usd ?? 0;
  const cap = S.spend.daily_budget || 0;
  $('spend').textContent = cap ? `$${day.toFixed(3)} / $${cap.toFixed(2)}`
                               : `$${day.toFixed(3)}`;
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
  if (quiet) return;
  if (!V.selected || ev.agent_id === V.selected) appendLine($('feed'), ev);
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
  if (!data.transcript.length) feed.innerHTML = '<p class="muted">Nothing on the record yet.</p>';
  for (const row of data.transcript) {
    appendLine(feed, { type: 'agent.' + row.kind, agent_id: id, ts: row.ts,
                       payload: { text: row.body } });
  }
}

function appendLine(feed, ev) {
  const kind = ev.type.split('.').pop();
  const who = ev.agent_id === 'user' ? { name: 'You', emoji: '🧑' }
    : (S.byId[ev.agent_id] || { name: ev.agent_id || 'office', emoji: '' });
  const p = ev.payload || {};

  let text, cls = kind;
  if (kind === 'tool') text = `${p.tool}${p.args ? ' · ' + p.args : ''}`;
  else if (kind === 'created') { text = `new task: ${p.title} → ${p.assignee}`; cls = 'tool'; }
  else if (kind === 'updated') { text = `task ${p.status}`; cls = 'tool'; }
  else if (kind === 'requested') { text = `needs approval: ${p.action}`; cls = 'error'; }
  else if (kind === 'decided') { text = `approval ${p.status}`; cls = 'tool'; }
  else if (kind === 'break') { text = `heads to the break room — "${p.line}"`; cls = 'social'; }
  else if (kind === 'return') { text = `back at their desk — "${p.line}"`; cls = 'social'; }
  else if (kind === 'summoned') { text = `called into the manager's office (${p.reason})`; cls = 'error'; }
  else if (kind === 'scold') { text = `Miles: "${p.line}"`; cls = 'error'; }
  else if (kind === 'dismissed') { text = `sent back to their desk`; cls = 'social'; }
  else if (kind === 'patrol') { text = `walks the floor`; cls = 'social'; }
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
