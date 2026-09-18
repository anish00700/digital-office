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
const T = 32, W = 29, H = 22;          // logical tiles
const LOGW = W * T, LOGH = H * T;      // 928 x 704

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
  markBackgroundDirty();     // desks are cut out of the baked wall layer
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

  // Sprites survive a reload so a hire or a desk move does not teleport
  // everyone back to their chair mid-walk.
  const present = new Set(S.roster.map(r => r.id));
  for (const id of Object.keys(V.sprites)) {
    if (present.has(id)) continue;
    delete V.sprites[id];
    delete S.agents[id];
    if (V.selected === id) V.selected = null;
  }
  for (const r of S.roster) {
    if (!S.agents[r.id]) S.agents[r.id] = { status: 'idle', detail: '' };
    const home = { x: r.desk[0], y: r.desk[1] + 1 };  // the seat, below the desk
    const sp = V.sprites[r.id];
    if (sp) { sp.home = home; continue; }
    V.sprites[r.id] = {
      id: r.id,
      x: home.x, y: home.y,
      home,
      path: [], script: [], wait: 0, walked: 0,
      dir: 'up', state: 'idle', activity: 'desk',
      bubble: null, thought: '', tool: '', toolAt: -99,
      // Per-person offset so the room never blinks or breathes in unison.
      phase: Math.random(), idleFor: 0, prop: ''
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
  renderRecipients();
  renderBoard(); renderApprovals(); renderSpend(); renderFeed();
  S.principal = state.principal || '';
  if (state.setup_needed) openSetup();
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

    // Someone was hired, edited, or let go - the floor plan itself changed.
    case 'roster.changed': applyRosterChange(ev); break;

    case 'task.created':
    case 'task.updated': refreshTasks(); pushFeed(ev); break;
    case 'approval.requested':
    case 'approval.decided': refreshApprovals(); pushFeed(ev); break;
    case 'usage':
      // The daemon sends every window the topbar might render. Writing only
      // .total here is what left the counter frozen at page-load values.
      if (p.spend) { Object.assign(S.spend, p.spend); renderSpend(); }
      if (USAGE.open) loadUsage();
      break;
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
      // The prop is picked up on arrival. Tying it to the activity meant a
      // full mug materialised in someone's hand the instant they stood up.
      sp.script = [{ type: 'goto', x: spot.x, y: spot.y },
                   { type: 'face', dir: 'down' },
                   { type: 'prop', prop: lookFor(ev.agent_id).vice }];
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
  sp.prop = '';                        // put the cup down before walking back
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
    // Idle time drives the tired face; anything else resets it.
    sp.idleFor = (sp.state === 'idle' && !sp.path.length) ? sp.idleFor + dt : 0;
    // Idle people glance around. Costs nothing, and a room where everyone
    // stares dead ahead reads as a shop window rather than an office.
    if (sp.idleFor > 2 && sp.activity === 'desk') {
      if (V.t > (sp.nextGlance || 0)) {
        sp.nextGlance = V.t + 7 + Math.random() * 13;
        sp.glanceUntil = V.t + 1.1 + Math.random() * 0.9;
        sp.glanceDir = Math.random() < 0.5 ? 'left' : 'right';
      }
    } else {
      sp.glanceUntil = 0;
    }
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
    if (step.type === 'prop') { sp.prop = step.prop; sp.script.shift(); continue; }
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
  // leaving a third of the pane empty. Below 1:1 there is no integer option
  // left, so scale smoothly instead of cropping the office in half.
  const raw = Math.min(canvas.width / LOGW, canvas.height / LOGH);
  V.crisp = raw >= 1;
  V.scale = V.crisp ? Math.floor(raw + 0.18) : raw;
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
  ctx.imageSmoothingEnabled = !V.crisp;
  ctx.fillStyle = '#1b1a24';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(world, 0, 0, LOGW, LOGH, V.ox, V.oy, V.dw, V.dh);
  ctx.setTransform(V.dpr, 0, 0, V.dpr, 0, 0);
}

/* ---- world buffer (pixel art) ---- */

/* The world is authored at 32 logical pixels per tile and blitted at an
 * integer device-pixel scale. Twice the old density, so a face gets real eyes
 * instead of two dark squares, and on most screens it lands at 1:1 - one art
 * pixel, one screen pixel, which is as crisp as this gets.
 *
 * Floors and walls never change, so they are baked once into their own buffer
 * and blitted; only furniture, desks and people are redrawn each frame. */

const bg = document.createElement('canvas');
bg.width = LOGW; bg.height = LOGH;
const bgx = bg.getContext('2d');
let bgDirty = true;
const markBackgroundDirty = () => { bgDirty = true; };

function paintWorld() {
  if (bgDirty) { paintBackground(); bgDirty = false; }
  g.clearRect(0, 0, LOGW, LOGH);
  g.drawImage(bg, 0, 0);

  clock(27 * T + 8, 12);                       // clocks tick, so they stay live
  clock(6 * T + 10, 14 * T + 8);
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

// Every drawing helper paints into `gc`. Pointing it at the background buffer
// for one pass is what lets the static layer reuse the same code as the frame.
let gc = g;
function px(x, y, w, h, color) { gc.fillStyle = color; gc.fillRect(x, y, w, h); }
function tile(tx, ty, color) { px(tx * T, ty * T, T, T, color); }

/* ---- static layer ---- */

function paintBackground() {
  gc = bgx;
  try {
    bgx.clearRect(0, 0, LOGW, LOGH);
    paintFloors();
    paintOcclusion();
    paintWalls();
    paintRoomLight();
  } finally {
    gc = g;
  }
}

/* Ambient occlusion. Light does not reach into the angle where the floor meets
 * a wall, and a flat floor butted against a flat wall is the single clearest
 * tell that a scene was assembled rather than lit. The gradient is deepest
 * below a wall, because the light in this room comes from above and to the
 * left, so that is the edge that sits in its own shadow. */
function paintOcclusion() {
  const solid = (x, y) =>
    x >= 0 && x < W && y >= 0 && y < H && SOLID[y * W + x];

  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      if (solid(x, y)) continue;
      const x0 = x * T, y0 = y * T;

      if (solid(x, y - 1))                       // wall above: deepest
        for (let i = 0; i < 14; i++)
          px(x0, y0 + i, T, 1, `rgba(14,11,22,${(0.34 * (1 - i / 14) ** 1.7).toFixed(3)})`);

      if (solid(x - 1, y))                       // wall left: lit side, softer
        for (let i = 0; i < 9; i++)
          px(x0 + i, y0, 1, T, `rgba(14,11,22,${(0.20 * (1 - i / 9) ** 1.7).toFixed(3)})`);

      if (solid(x + 1, y))                       // wall right: away from light
        for (let i = 0; i < 11; i++)
          px(x0 + T - 1 - i, y0, 1, T, `rgba(14,11,22,${(0.26 * (1 - i / 11) ** 1.7).toFixed(3)})`);

      if (solid(x, y + 1))                       // wall below: faint
        for (let i = 0; i < 7; i++)
          px(x0, y0 + T - 1 - i, T, 1, `rgba(14,11,22,${(0.14 * (1 - i / 7) ** 1.7).toFixed(3)})`);

      // Inside corners collect a little more than either edge alone.
      if (solid(x - 1, y) && solid(x, y - 1)) px(x0, y0, 8, 8, 'rgba(14,11,22,.16)');
      if (solid(x + 1, y) && solid(x, y - 1)) px(x0 + T - 8, y0, 8, 8, 'rgba(14,11,22,.16)');
    }
  }
}

/* One warm pool per ceiling lamp, and a cool fall-off everywhere else. Two
 * colour temperatures in the same room is most of what separates a lit scene
 * from an evenly filled one. */
function paintRoomLight() {
  for (const [lx, ly] of CEILING_LIGHTS) {
    const cx = lx * T + T / 2, cy = ly * T + T / 2 + 10;
    for (let r = 7; r >= 1; r--) {
      const rad = r * T * 0.9;
      px(cx - rad, cy - rad, rad * 2, rad * 2,
         `rgba(255,226,168,${(0.020 * (1 - r / 8)).toFixed(4)})`);
    }
  }
  // Vignette: the corners of a room are always darker than the middle of it.
  for (let i = 0; i < 5; i++) {
    const inset = i * 14;
    const a = (0.05 * (1 - i / 5)).toFixed(3);
    px(0, inset, LOGW, 14, `rgba(10,8,16,${a})`);
    px(0, LOGH - inset - 14, LOGW, 14, `rgba(10,8,16,${a})`);
    px(inset, 0, 14, LOGH, `rgba(10,8,16,${a})`);
    px(LOGW - inset - 14, 0, 14, LOGH, `rgba(10,8,16,${a})`);
  }
}

function paintFloors() {
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const left = x < OPEN_PLAN_X;
      if (left && y < 9) paintWoodTile(x, y);
      else if (left && y > 13) paintCeramicTile(x, y);
      else if (left) paintCarpetTile(x, y, '#6f6a63', '#75706a');
      else paintCarpetTile(x, y, '#6d675f', '#706a62');
    }
  }

  // Ceiling lights. Soft pools give the open plan depth and stop the carpet
  // reading as one flat colour across half the screen.
  for (const [lx, ly] of CEILING_LIGHTS) {
    for (let ring = 5; ring >= 1; ring--) {
      px(lx * T - ring * T, ly * T - ring * T + 8,
         (ring * 2 + 1) * T, (ring * 2 + 1) * T, 'rgba(255,240,205,.018)');
    }
  }

  // Large-scale mottling. The 4px weave gives texture but every tile still
  // carries the same average value, and a floor of identical tiles reads as
  // wallpaper. These slow patches vary the value across whole regions.
  for (let i = 0; i < 260; i++) {
    const bx = (i * 137 + 11) % LOGW, by = (i * 241 + 47) % LOGH;
    const bw = 24 + (i * 13) % 70, bh = 20 + (i * 7) % 54;
    if (bx < OPEN_PLAN_X * T && by > 9 * T && by < 13 * T) continue;
    px(bx, by, bw, bh, i % 3 === 0 ? 'rgba(255,240,214,.014)'
                                   : 'rgba(18,14,28,.016)');
  }
  // Traffic wear: the routes people actually walk get lighter over time.
  for (let x = OPEN_PLAN_X + 1; x < W - 1; x++)
    px(x * T, 8 * T + 6, T, 40, 'rgba(255,238,208,.020)');
  for (let y = 1; y < H - 1; y++)
    px(11 * T + 4, y * T, 36, T, 'rgba(255,238,208,.016)');

  // Break-room rug, woven rather than a flat rectangle.
  const rx = 1 * T, ry = 16 * T, rw = 7 * T, rh = 4 * T;
  px(rx, ry, rw, rh, 'rgba(150,90,70,.30)');
  px(rx, ry, rw, 3, 'rgba(196,136,104,.40)');
  px(rx, ry + rh - 3, rw, 3, 'rgba(104,56,42,.40)');
  px(rx, ry, 3, rh, 'rgba(196,136,104,.24)');
  px(rx + rw - 3, ry, 3, rh, 'rgba(104,56,42,.30)');
  for (let i = 0; i < 14; i++)
    px(rx + 8 + i * 15, ry + 14, 9, 3, 'rgba(198,148,116,.16)');
  for (let i = 0; i < 14; i++)
    px(rx + 14 + i * 15, ry + rh - 20, 9, 3, 'rgba(198,148,116,.12)');
}

function paintWoodTile(x, y) {
  const x0 = x * T, y0 = y * T;
  px(x0, y0, T, T, (x + y) % 2 ? '#9a7a50' : '#93744c');
  // Planks run the length of the room; the seam is what sells it as wood.
  for (let p = 0; p < 2; p++) {
    const py = y0 + p * 16;
    px(x0, py, T, 1, 'rgba(255,232,190,.09)');
    px(x0, py + 15, T, 1, 'rgba(74,52,32,.26)');
  }
  for (let i = 0; i < 5; i++) {                // grain
    const gx = x0 + ((x * 13 + i * 7) % T);
    const gy = y0 + ((y * 11 + i * 6) % T);
    px(gx, gy, 3 + (i % 3), 1, 'rgba(112,82,50,.22)');
  }
  if ((x * 7 + y * 3) % 5 === 0) px(x0 + 6, y0 + 9, 2, 2, 'rgba(90,64,38,.30)');
}

function paintCeramicTile(x, y) {
  const x0 = x * T, y0 = y * T;
  const base = (x + y) % 2 ? '#8b9c99' : '#7e8f8c';
  px(x0, y0, T, T, base);
  // Four ceramic tiles per floor tile, with grout between - a finer grid than
  // the world grid, which is the whole point of the higher resolution.
  for (let sy = 0; sy < 2; sy++) for (let sx = 0; sx < 2; sx++) {
    const tx = x0 + sx * 16, ty = y0 + sy * 16;
    px(tx, ty, 15, 15, (sx + sy) % 2 ? shade(base, 8) : shade(base, -5));
    px(tx, ty, 15, 1, 'rgba(255,255,255,.10)');
    px(tx, ty + 14, 15, 1, 'rgba(0,0,0,.10)');
  }
  px(x0, y0 + 15, T, 1, 'rgba(60,70,70,.35)');
  px(x0 + 15, y0, 1, T, 'rgba(60,70,70,.35)');
}

function paintCarpetTile(x, y, a, b) {
  const x0 = x * T, y0 = y * T;
  px(x0, y0, T, T, (x + y) % 2 ? a : b);
  // Loop-pile weave: a 4px dither that reads as texture rather than as a grid.
  for (let sy = 0; sy < T; sy += 4) {
    for (let sx = 0; sx < T; sx += 4) {
      const n = ((x * 31 + sx) * 17 + (y * 13 + sy) * 7) % 9;
      if (n < 3) px(x0 + sx, y0 + sy, 2, 2, 'rgba(0,0,0,.035)');
      else if (n > 6) px(x0 + sx + 1, y0 + sy + 1, 2, 2, 'rgba(255,255,255,.022)');
    }
  }
}

function paintWalls() {
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      if (!SOLID[y * W + x]) continue;
      if (isFurniture(x, y)) continue;
      const x0 = x * T, y0 = y * T;

      if (x === OPEN_PLAN_X && y <= 9) {                 // the glass partition
        px(x0, y0, T, T, 'rgba(150,200,215,.14)');
        px(x0 + 11, y0, 9, T, 'rgba(190,225,240,.26)');  // a reflected highlight
        px(x0 + 3, y0, 2, T, 'rgba(190,225,240,.12)');
        px(x0, y0, T, 3, '#5b6d75');
        px(x0, y0 + T - 2, T, 2, 'rgba(40,54,60,.5)');
        continue;
      }

      // Walls are a material, not a fill: a lit top cap, a face that falls off
      // as it descends, a rail, and a skirting board that casts onto the floor.
      const base = '#3c3a48';
      px(x0, y0, T, T, base);
      px(x0, y0, T, 3, shade(base, 38));                 // top cap catches light
      px(x0, y0 + 3, T, 2, shade(base, 20));
      px(x0, y0 + 5, T, 1, 'rgba(255,255,255,.06)');
      for (let i = 0; i < T - 6; i++)                    // vertical fall-off
        px(x0, y0 + 6 + i, T, 1, `rgba(16,13,26,${(i / (T - 6) * 0.22).toFixed(3)})`);
      px(x0, y0 + 13, T, 1, shade(base, 16));            // dado rail
      px(x0, y0 + 14, T, 1, 'rgba(16,13,26,.20)');

      for (let i = 0; i < 7; i++) {                      // plaster grain
        const gx = x0 + ((x * 17 + i * 11) % T);
        const gy = y0 + 7 + ((y * 7 + i * 5) % (T - 12));
        px(gx, gy, 2, 1, `rgba(255,255,255,${i % 2 ? 0.022 : 0.014})`);
      }
      // A seam every two tiles reads as panelling and stops long walls from
      // looking like one extruded block.
      if (x % 2 === 0) {
        px(x0, y0 + 5, 1, T - 10, 'rgba(16,13,26,.22)');
        px(x0 + 1, y0 + 5, 1, T - 10, 'rgba(255,255,255,.028)');
      }

      if (!SOLID[(y + 1) * W + x] || y === H - 1) {
        px(x0, y0 + T - 11, T, 7, shade(base, 22));      // skirting board
        px(x0, y0 + T - 11, T, 1, shade(base, 46));
        px(x0, y0 + T - 5, T, 1, shade(base, -30));
        px(x0, y0 + T - 4, T, 4, '#211f2a');             // shadow it casts
      }
    }
  }
  doorway(MGR_DOOR.x, MGR_DOOR.y, 'v');
  doorway(BREAK_DOOR.x, BREAK_DOOR.y, 'h');
  paintWallArt();

  roomSign(2, 0, 'MANAGER');
  roomSign(2, 13, 'BREAK ROOM');
}

// Things hung on the walls. Drawn after the wall pass so they sit on top of
// it, which means they need no map changes and block nobody's path.
const CEILING_LIGHTS = [[15, 3], [23, 3], [15, 12], [23, 12], [19, 8]];

function paintWallArt() {
  const frame = (x, y, w, h, art) => {
    px(x - 1, y - 1, w + 2, h + 2, 'rgba(0,0,0,.35)');
    px(x, y, w, h, '#2a2630');
    px(x + 2, y + 2, w - 4, h - 4, art);
    px(x + 2, y + 2, w - 4, 1, 'rgba(255,255,255,.20)');
    px(x + 2, y + h - 3, w - 4, 1, 'rgba(0,0,0,.30)');
  };

  // Windows along the top of the open plan - night sky over a lit city.
  for (const wx of [13, 18, 23]) {
    const x = wx * T, y = 4, w = T * 2, h = T - 12;
    px(x - 2, y - 2, w + 4, h + 4, '#2f2d3a');
    px(x, y, w, h, '#10151f');
    for (let i = 0; i < h; i++)                          // sky gradient
      px(x, y + i, w, 1, `rgba(46,68,110,${0.55 - i / h * 0.42})`);
    for (let b = 0; b < 6; b++) {                        // building silhouettes
      const bw = 7 + ((b * 5 + wx) % 9);
      const bh = 6 + ((b * 11 + wx * 3) % 13);
      const bx = x + 2 + b * 10;
      if (bx + bw > x + w - 2) continue;
      px(bx, y + h - bh, bw, bh, '#0c1017');
      for (let ly = 0; ly < bh - 2; ly += 3)             // lit windows
        for (let lx = 0; lx < bw - 2; lx += 3)
          if (((lx + ly + b * 7 + wx) % 5) < 2)
            px(bx + 1 + lx, y + h - bh + 1 + ly, 1, 2, 'rgba(255,222,150,.60)');
    }
    px(x, y, w, 3, '#4b4959');                           // frame
    px(x, y + h - 3, w, 3, '#4b4959');
    px(x + w / 2 - 1, y, 3, h, '#4b4959');               // mullion
    px(x, y, 3, h, '#565463'); px(x + w - 3, y, 3, h, '#565463');
    px(x + 3, y + 3, w - 6, 1, 'rgba(255,255,255,.10)');
  }

  // Manager's office: a diploma and a rather serious painting.
  frame(2 * T + 4, 8, 24, 20, '#7d6a45');
  px(2 * T + 9, 15, 14, 1, 'rgba(255,255,255,.30)');
  px(2 * T + 9, 19, 10, 1, 'rgba(255,255,255,.20)');
  frame(5 * T + 2, 6, 28, 24, '#2f4a3e');
  px(5 * T + 6, 20, 20, 8, '#3f5a4a');                   // hills
  px(5 * T + 6, 12, 20, 5, '#54707f');                   // sky band
  px(5 * T + 19, 11, 4, 4, 'rgba(240,225,180,.5)');      // a small sun

  // Open plan: a whiteboard nobody has cleaned.
  const bx = 20 * T, by = 4, bw = T * 3, bh = T - 12;
  px(bx - 2, by - 2, bw + 4, bh + 4, '#8e8a80');
  px(bx, by, bw, bh, '#dedbd2');
  px(bx, by, bw, 2, '#f0eee8');
  px(bx, by + bh - 3, bw, 3, '#a8a49a');
  px(bx + 8, by + 7, 40, 2, 'rgba(64,104,164,.75)');
  px(bx + 8, by + 13, 56, 2, 'rgba(64,104,164,.55)');
  px(bx + 8, by + 19, 28, 2, 'rgba(186,66,56,.65)');
  px(bx + 52, by + 17, 24, 6, 'rgba(64,134,86,.45)');
  px(bx + 52, by + 17, 24, 1, 'rgba(64,134,86,.7)');
  px(bx + bw - 22, by + bh - 8, 14, 3, '#b8433d');       // a marker on the tray

  // Break room poster.
  frame(3 * T + 4, 13 * T + 6, 26, 22, '#3f5666');
  px(3 * T + 9, 13 * T + 14, 16, 3, 'rgba(255,255,255,.22)');
  px(3 * T + 9, 13 * T + 20, 10, 2, 'rgba(255,255,255,.14)');
}

function clock(x, y) {
  px(x - 1, y - 1, 22, 22, 'rgba(0,0,0,.30)');
  px(x, y, 20, 20, '#c9c3b6');
  px(x + 2, y + 2, 16, 16, '#f4f0e6');
  px(x + 2, y + 2, 16, 1, '#ffffff');
  px(x, y, 20, 2, '#ddd7ca');
  for (let i = 0; i < 12; i++) {                         // hour ticks
    const a = i / 12 * 6.283;
    px(Math.round(x + 10 + Math.cos(a) * 7) - 1,
       Math.round(y + 10 + Math.sin(a) * 7) - 1, 2, 2,
       i % 3 === 0 ? '#4a4550' : 'rgba(74,69,80,.45)');
  }
  // Real time - a stopped clock in an office that runs all night is a lie.
  const now = new Date();
  const hA = ((now.getHours() % 12) + now.getMinutes() / 60) / 12 * 6.283 - 1.571;
  const mA = (now.getMinutes() + now.getSeconds() / 60) / 60 * 6.283 - 1.571;
  const cx = x + 10, cy = y + 10;
  for (let i = 1; i <= 5; i++)
    px(Math.round(cx + Math.cos(hA) * i), Math.round(cy + Math.sin(hA) * i), 2, 2, '#2a2630');
  for (let i = 1; i <= 7; i++)
    px(Math.round(cx + Math.cos(mA) * i), Math.round(cy + Math.sin(mA) * i), 1, 1, '#514b5c');
  px(cx - 1, cy - 1, 3, 3, '#b8433d');
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
  const x0 = x * T, y0 = y * T;
  if (dir === 'v') {
    px(x0, y0, 6, T, '#6b5330'); px(x0 + T - 6, y0, 6, T, '#6b5330');
    px(x0 + 1, y0, 2, T, '#8a6d43'); px(x0 + T - 5, y0, 2, T, '#8a6d43');
    px(x0 + 6, y0, T - 12, 3, 'rgba(0,0,0,.25)');
  } else {
    px(x0, y0, T, 6, '#6b5330'); px(x0, y0 + T - 6, T, 6, '#6b5330');
    px(x0, y0 + 1, T, 2, '#8a6d43'); px(x0, y0 + T - 5, T, 2, '#8a6d43');
    px(x0, y0 + 6, 3, T - 12, 'rgba(0,0,0,.25)');
  }
}

function roomSign(tx, ty, text) {
  gc.fillStyle = 'rgba(255,255,255,.36)';
  gc.font = '13px ui-monospace, Menlo, monospace';
  gc.textAlign = 'left'; gc.textBaseline = 'top';
  gc.fillText(text, tx * T, ty * T + 9);
}

function paintDesk(r) {
  const [tx, ty] = r.desk;
  const x = (tx - 1) * T, y = ty * T;
  const sp = V.sprites[r.id];
  const lit = sp.state === 'working' || sp.state === 'thinking';

  const wood = '#6b4f34';
  const woodLit = shade(wood, 40);
  const woodTop = shade(wood, 22);
  const woodDark = shade(wood, -30);
  const woodDeep = shade(wood, -56);

  // The monitor throws light back onto the desk. Drawn under everything else
  // so the clutter sits inside the pool rather than on top of a flat slab.
  if (lit) {
    px(x + 20, y - 6, 56, 22, 'rgba(126,182,232,.075)');
    px(x + 30, y - 3, 36, 15, 'rgba(150,200,242,.070)');
    px(x + 38, y, 20, 9, 'rgba(176,216,248,.060)');
  }

  /* ---- the desk, as a slab with a visible edge ---- */
  px(x + 2, y + T - 6, T * 3 - 4, 8, 'rgba(14,11,22,.30)');   // cast shadow
  px(x + 6, y + T + 1, T * 3 - 12, 4, 'rgba(14,11,22,.18)');

  px(x + 1, y + 3, T * 3 - 2, 1, woodLit);                    // chamfered corners
  px(x, y + 4, T * 3, T - 11, wood);
  px(x, y + 4, T * 3, 3, woodTop);                            // top plane, lit
  px(x, y + 4, T * 3, 1, woodLit);
  px(x, y + 7, T * 3, 1, 'rgba(255,240,208,.14)');
  px(x, y + 4, 3, T - 11, shade(wood, 12));                   // lit left end
  px(x + T * 3 - 3, y + 4, 3, T - 11, woodDark);              // shaded right end

  for (let i = 0; i < 12; i++)                                // grain
    px(x + 4 + i * 8, y + 11, 5 + (i % 3) * 4, 1, 'rgba(74,53,36,.24)');
  for (let i = 0; i < 6; i++)
    px(x + 14 + i * 15, y + 17, 8, 1, 'rgba(96,70,46,.28)');

  px(x, y + T - 7, T * 3, 3, woodDark);                       // front edge
  px(x, y + T - 4, T * 3, 2, woodDeep);
  px(x + 4, y + T - 2, 6, 11, woodDeep);                      // legs
  px(x + T * 3 - 10, y + T - 2, 6, 11, woodDeep);
  px(x + 4, y + T - 2, 2, 11, woodDark);

  /* ---- monitor ---- */
  const mx = x + 34, my = y - 24;
  px(mx - 4, my - 2, 35, 32, 'rgba(14,11,22,.34)');           // shadow it casts
  px(mx - 2, my - 2, 31, 29, '#15141c');                      // bezel
  px(mx - 2, my - 2, 31, 2, '#33313f');                       // lit top of bezel
  px(mx - 2, my - 2, 2, 29, '#28262f');
  px(mx + 27, my - 2, 2, 29, '#0e0d13');
  px(mx, my, 27, 24, '#1d1c24');
  const screen = lit ? shade(r.color, -50) : '#2b303a';
  px(mx + 1, my + 1, 25, 22, screen);
  if (lit) {
    // Scrolling "code" in their own colour - reads as work from across the room.
    const t = Math.floor(V.t * 2.2 + tx);
    for (let i = 0; i < 7; i++) {
      const w = 4 + ((t + i * 5 + tx * 3) % 19);
      const active = i === (t % 7);
      px(mx + 3, my + 3 + i * 3, 2, 2, shade(r.color, 70));
      px(mx + 7, my + 3 + i * 3, Math.min(w, 17), 2,
         active ? shade(r.color, 88) : shade(r.color, 24));
    }
    if (Math.floor(V.t * 2.6) % 2) px(mx + 8, my + 21, 5, 2, shade(r.color, 96));
    px(mx + 1, my + 1, 25, 1, shade(r.color, 40));            // screen glow spill
  } else {
    px(mx + 3, my + 6, 19, 2, 'rgba(255,255,255,.045)');
    px(mx + 3, my + 11, 12, 2, 'rgba(255,255,255,.030)');
  }
  px(mx + 1, my + 1, 12, 1, 'rgba(255,255,255,.13)');         // glass sheen
  px(mx + 1, my + 2, 5, 1, 'rgba(255,255,255,.07)');
  px(mx + 11, my + 24, 5, 5, '#1a1922');                      // stand
  px(mx + 11, my + 24, 2, 5, '#2b2934');
  px(mx + 6, my + 29, 15, 3, '#15141c');
  px(mx + 6, my + 29, 15, 1, '#35333f');

  /* ---- desk clutter ---- */
  px(x + 29, y + 16, 34, 8, 'rgba(14,11,22,.26)');            // keyboard shadow
  px(x + 30, y + 15, 32, 7, '#2f2e39');
  px(x + 30, y + 15, 32, 1, '#4a4857');
  px(x + 30, y + 21, 32, 1, '#1e1d26');
  for (let i = 0; i < 9; i++)
    px(x + 32 + i * 3, y + 17, 2, 2, '#3f3e4d');
  for (let i = 0; i < 8; i++)
    px(x + 33 + i * 3, y + 20, 2, 1, '#3f3e4d');
  px(x + 66, y + 16, 7, 6, '#2f2e39');
  px(x + 66, y + 16, 7, 1, '#45444f');

  px(x + 78, y + 15, 13, 4, 'rgba(14,11,22,.24)');            // mug shadow
  px(x + 79, y + 6, 11, 12, '#ddd7ca');
  px(x + 79, y + 6, 3, 12, '#f4efe4');                        // lit side
  px(x + 87, y + 6, 3, 12, '#b3ab9b');                        // shaded side
  px(x + 79, y + 6, 11, 1, '#fbf7ec');
  px(x + 79, y + 16, 11, 2, '#a09889');
  px(x + 90, y + 9, 3, 6, '#c9c1b4');                         // handle
  px(x + 81, y + 8, 7, 3, '#5f3d24');                         // coffee
  px(x + 81, y + 8, 7, 1, '#7d5335');

  px(x + 6, y + 17, 20, 4, 'rgba(14,11,22,.22)');             // paper shadow
  px(x + 8, y + 8, 17, 11, '#eae4d6');
  px(x + 6, y + 10, 17, 11, '#ddd7c9');
  px(x + 6, y + 10, 17, 1, '#f6f1e4');
  px(x + 6, y + 10, 2, 11, '#f0ebde');
  px(x + 21, y + 10, 2, 11, '#bfb9ab');
  px(x + 9, y + 14, 11, 1, 'rgba(90,80,70,.40)');
  px(x + 9, y + 17, 8, 1, 'rgba(90,80,70,.30)');

  // A desk plant on every third desk, so the room is not uniform.
  if ((tx + ty) % 3 === 0) {
    px(x + 84, y - 2, 13, 4, 'rgba(14,11,22,.24)');
    px(x + 85, y - 12, 11, 11, '#8a5a3c');
    px(x + 85, y - 12, 3, 11, '#a8764e');
    px(x + 93, y - 12, 3, 11, '#6b4530');
    px(x + 85, y - 12, 11, 2, '#b3804f');
    px(x + 86, y - 14, 9, 3, '#5c3c28');
    px(x + 88, y - 24, 5, 11, '#4e7a44');
    px(x + 88, y - 24, 2, 11, '#639a56');
    px(x + 84, y - 20, 5, 7, '#3f6838');
    px(x + 92, y - 21, 5, 8, '#375c31');
    px(x + 89, y - 27, 4, 5, '#6aa85c');
  }
}

/* ---------------------------------------------------------------- casting -- */

// Everyone gets a fixed face so you learn who is who at a glance. The seeded
// staff are cast by hand; anyone hired later is derived from their id, which
// is stable for as long as they work here.
const SKINS = ['#f4d3ae', '#eab98d', '#d79c6c', '#b87c50', '#96603a', '#6f4629'];
const HAIRS = ['#241a14', '#3d2a1c', '#5c3a20', '#8a5a2b', '#b98b45', '#dcc184',
               '#8c3b2a', '#7d7a82', '#c9c4bc', '#3a3f52'];
const STYLES = ['short', 'messy', 'ponytail', 'bun', 'long', 'curls', 'cap', 'bald'];
const FACIAL = ['none', 'none', 'none', 'none', 'stubble', 'beard', 'moustache'];
const ACCS = ['none', 'none', 'none', 'glasses', 'glasses', 'headphones', 'earrings'];
// What someone does with a break, and what they do with their hands when
// there is nothing to do. Neither costs a token: it is all client-side.
const VICES = ['coffee', 'coffee', 'coffee', 'tea', 'smoke', 'snack', 'phone'];
const HABITS = ['still', 'still', 'lean', 'tap', 'stretch', 'swivel'];

const CAST = {
  // Vices and habits are cast, not random: the manager who chain-smokes and
  // the writer who never leaves his chair are characters, not noise.
  manager:    { skin: 2, hair: '#7d7a82', style: 'short',    facial: 'stubble', acc: 'none',       vice: 'smoke',  habit: 'lean' },
  sre:        { skin: 1, hair: '#3d2a1c', style: 'ponytail', facial: 'none',    acc: 'glasses',    vice: 'coffee', habit: 'tap' },
  pipeline:   { skin: 3, hair: '#241a14', style: 'messy',    facial: 'none',    acc: 'headphones', vice: 'phone',  habit: 'swivel' },
  comms:      { skin: 0, hair: '#8c3b2a', style: 'long',     facial: 'none',    acc: 'earrings',   vice: 'tea',    habit: 'still' },
  researcher: { skin: 4, hair: '#241a14', style: 'bun',      facial: 'none',    acc: 'glasses',    vice: 'coffee', habit: 'stretch' },
  scheduler:  { skin: 2, hair: '#5c3a20', style: 'cap',      facial: 'none',    acc: 'none',       vice: 'snack',  habit: 'swivel' },
  writer:     { skin: 1, hair: '#4a3122', style: 'curls',    facial: 'beard',   acc: 'none',       vice: 'coffee', habit: 'lean' },
  analyst:    { skin: 5, hair: '#241a14', style: 'short',    facial: 'none',    acc: 'glasses',    vice: 'tea',    habit: 'tap' },
  hr:         { skin: 1, hair: '#b98b45', style: 'bun',      facial: 'none',    acc: 'none',       vice: 'tea',    habit: 'still' },
  critic:     { skin: 3, hair: '#7d7a82', style: 'short',    facial: 'beard',   acc: 'glasses',    vice: 'smoke',  habit: 'lean' },
};

const _looks = {};

/** Stable per-employee appearance. Hand-cast where we have one, hashed otherwise. */
function lookFor(id) {
  if (_looks[id]) return _looks[id];
  let look = CAST[id];
  if (!look) {
    let h = 0;
    for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
    // >>> not >>: the hash is a full 32 bits, and a signed shift on anything
    // with the high bit set goes negative, indexes the palette off the front,
    // and hands an undefined colour to shade() - which takes the whole floor
    // down on the next frame.
    const pick = (arr, salt) => arr[(h >>> salt) % arr.length];
    look = {
      skin: (h >>> 3) % SKINS.length,
      hair: pick(HAIRS, 7),
      style: pick(STYLES, 11),
      facial: pick(FACIAL, 17),
      acc: pick(ACCS, 21),
      vice: pick(VICES, 5),
      habit: pick(HABITS, 13),
    };
  }
  // Normalise before caching. A look is data that reaches shade(), and one
  // undefined colour blanks the entire floor on the next frame - so nothing
  // leaves here that the renderer cannot draw.
  const safe = {
    skin: SKINS[look.skin] ? look.skin : 0,
    hair: HAIRS.includes(look.hair) || /^#[0-9a-f]{6}$/i.test(look.hair || '')
          ? look.hair : HAIRS[0],
    style: STYLES.includes(look.style) ? look.style : 'short',
    facial: FACIAL.includes(look.facial) ? look.facial : 'none',
    acc: ACCS.includes(look.acc) ? look.acc : 'none',
    vice: VICES.includes(look.vice) ? look.vice : 'coffee',
    habit: HABITS.includes(look.habit) ? look.habit : 'still',
  };
  _looks[id] = { ...safe, skinHex: SKINS[safe.skin] };
  return _looks[id];
}

/* Which face to wear. Ambient state beats work state: someone who has just
 * been told off looks it, whatever their task queue says. */
function expressionFor(r, sp) {
  if (sp.activity === 'scolded') return r.manager ? 'cross' : 'sad';
  if (sp.activity === 'patrol') return r.manager ? 'stern' : 'neutral';
  if (sp.activity === 'break') return 'pleased';
  if (sp.state === 'blocked') return 'worried';
  if (sp.state === 'thinking') return 'think';
  if (sp.state === 'working') return 'focus';
  if (sp.state === 'waiting') return 'bored';
  if (sp.idleFor > 26) return 'tired';
  return 'neutral';
}

// What the face is saying, in words, for the tooltip.
const MOODS = {
  neutral: '', focus: 'heads down', think: 'turning it over',
  worried: 'rattled - waiting on you', sad: 'just been told off',
  pleased: 'perked up', tired: 'flagging', bored: 'waiting it out',
  stern: 'unimpressed', cross: 'furious',
};

const INK = '#241f2e';
// The iris is never as dark as the lash line; flattening the two is what
// turns a pair of eyes into a pair of holes.
const IRIS = '#3d3550';
const OUTLINE = 'rgba(20,16,26,.90)';

/* Person: 22 wide, 52 tall, feet at (0,0) of the given point. At this density
 * a face gets a brow, a nose and a mouth that can actually change shape. */
const typingNow = (sp) =>
  seated(sp) && sp.activity === 'desk'
  && (sp.state === 'working' || sp.state === 'thinking');

function paintPerson(r, sp) {
  const cx = Math.round(sp.x * T + T / 2);
  const cy = Math.round(sp.y * T + T - 4);
  const look = lookFor(r.id);
  const sitting = seated(sp) && sp.activity === 'desk';
  const walking = sp.path.length > 0;
  const step = walking ? Math.floor(sp.walked * 4) % 4 : 0;
  const bob = walking ? (step === 1 || step === 3 ? -2 : 0) : 0;
  // Seated idlers breathe. It is two pixels, and it is the difference between
  // a room of people and a room of furniture.
  const breath = (!walking && Math.floor(V.t * 1.4 + sp.phase * 6) % 2) ? -1 : 0;

  // Sitting: face the room. Physically they would face the monitor, but a floor
  // of turned backs tells you nothing - the face is the whole point.
  const facing = sitting
    ? (V.t < (sp.glanceUntil || 0) ? sp.glanceDir : 'down')
    : sp.dir;

  // One light source for the whole office: a warm lamp above and to the LEFT.
  // Every surface below is lit on its left, shaded on its right, and carries a
  // thin cool bounce on the far edge. Consistency is what sells the volume.
  const skin = look.skinHex;
  const skinLit = shade(skin, 26);
  const skinMid = shade(skin, -12);
  const skinDark = shade(skin, -40);
  const skinBounce = shade(skin, -22);
  const cloth = r.color;
  const clothLit = shade(cloth, 34);
  const clothMid = shade(cloth, -14);
  const clothDark = shade(cloth, -44);
  const clothBounce = shade(cloth, -26);

  // An idle habit: something to do with the body when there is no work. Slow
  // and small on purpose - a floor of people fidgeting in sync reads as a
  // glitch rather than as character.
  let lean = 0, sway = 0;
  if (sitting && !typingNow(sp) && sp.idleFor > 3) {
    const beat = V.t * 0.5 + sp.phase * 7;
    if (look.habit === 'lean') lean = Math.sin(beat) > 0.75 ? 2 : 0;
    else if (look.habit === 'tap') lean = 0;
    else if (look.habit === 'stretch') lean = Math.sin(beat * 0.6) > 0.93 ? -3 : 0;
    else if (look.habit === 'swivel') sway = Math.round(Math.sin(beat * 0.8) * 1.6);
  }

  const y0 = cy + (sitting ? 6 : 0) + bob + (sitting ? breath : 0) + lean;
  const P = (dx, dy, w, h, c) => px(cx + dx + sway, y0 + dy, w, h, c);
  // The contact shadow is cast on the floor, so it must not lean or swivel
  // with the body - it stays put while the person moves over it.
  const G = (dx, dy, w, h, c) => px(cx + dx, cy + dy, w, h, c);

  /* ---- cast shadow: an ellipse, not a slab, and pinned to the floor ---- */
  const gy = sitting ? 6 : 0;
  G(-12, gy - 3, 24, 5, 'rgba(16,12,24,.20)');
  G(-14, gy - 2, 28, 3, 'rgba(16,12,24,.16)');
  G(-9, gy - 4, 18, 2, 'rgba(16,12,24,.13)');

  /* ---- legs / chair ---- */
  if (sitting) {
    const back = shade(cloth, -58);
    P(-15, -27, 30, 1, shade(cloth, -34));               // rounded chair top
    P(-16, -26, 32, 2, shade(cloth, -30));
    P(-17, -24, 34, 22, back);
    P(-17, -24, 3, 22, shade(cloth, -44));               // lit edge
    P(14, -24, 3, 22, shade(cloth, -72));                // shaded edge
    P(15, -24, 2, 22, shade(cloth, -60));                // bounce
    for (let i = 0; i < 3; i++)                          // seam padding
      P(-13, -20 + i * 6, 26, 1, shade(cloth, -70));
    P(-15, -5, 30, 4, '#2a2733');                        // seat
    P(-15, -5, 30, 1, '#3d3949');
    P(-15, -2, 30, 2, '#1e1c26');
  } else {
    const swing = step === 1 ? 2 : step === 3 ? -2 : 0;
    const trouser = '#3b3852', trouserLit = '#4a4766', trouserDark = '#2a2840';
    P(-8, -17 + Math.max(0, swing), 7, 15, trouser);
    P(1, -17 + Math.max(0, -swing), 7, 15, trouser);
    P(-8, -17 + Math.max(0, swing), 2, 15, trouserLit);  // lit left edge
    P(6, -17 + Math.max(0, -swing), 2, 15, trouserDark);
    P(-2, -16, 2, 14, trouserDark);                      // gap between legs
    P(-9, -3, 9, 3, '#211f2c');                          // shoes
    P(0, -3, 9, 3, '#211f2c');
    P(-9, -3, 9, 1, '#33303f');
  }

  /* ---- torso: sloped shoulders and a lit side ---- */
  // The outline follows the silhouette rather than boxing it, so the shoulders
  // read as shoulders instead of as a crate.
  P(-12, -37, 24, 1, OUTLINE);
  P(-14, -36, 28, 21, OUTLINE);
  P(-11, -36, 22, 1, clothLit);                          // shoulder line
  P(-13, -35, 26, 19, cloth);
  P(-13, -35, 26, 2, clothLit);                          // top plane catches light
  P(-13, -35, 4, 19, shade(cloth, 14));                  // lit left flank
  P(9, -35, 4, 19, clothDark);                           // shaded right flank
  P(12, -33, 1, 15, clothBounce);                        // cool bounce edge
  P(-13, -19, 26, 3, clothDark);                         // hem in shadow
  P(-13, -17, 26, 1, shade(cloth, -58));
  // Fold shadows under the arms - cloth does not hang flat.
  P(-10, -30, 2, 11, clothMid);
  P(7, -30, 2, 11, shade(cloth, -30));

  if (facing === 'down') {
    P(-5, -36, 10, 3, clothDark);                        // collar opening
    P(-4, -35, 8, 2, shade(skin, -46));                  // throat in shadow
    P(-1, -32, 2, 14, clothMid);                         // placket
    P(-2, -32, 1, 14, clothLit);
    P(-1, -29, 2, 1, shade(cloth, 52));                  // buttons
    P(-1, -24, 2, 1, shade(cloth, 52));
  }
  if (r.manager) {                                       // the tie of office
    P(-2, -32, 4, 12, '#a83c37');
    P(-2, -32, 1, 12, '#c65b52');
    P(1, -32, 1, 12, '#7e2a26');
    P(-2, -32, 4, 2, '#c65b52');
    P(-1, -20, 2, 3, '#6f2521');
  }

  /* ---- arms ---- */
  const typing = typingNow(sp);        // one definition, shared with the habits
  const armY = typing ? -26 : -32;
  if (facing !== 'up') {
    P(-17, armY, 4, 13, shade(cloth, -6));               // left arm is lit
    P(-17, armY, 1, 13, clothLit);
    P(-14, armY, 1, 13, clothMid);
  }
  P(13, armY, 4, 13, clothDark);                         // right arm is not
  P(16, armY + 1, 1, 11, clothBounce);
  P(13, armY, 4, 1, clothMid);
  if (typing) {                                          // hands on the keys
    const t = Math.floor(V.t * 9 + sp.phase * 3) % 2;
    P(-14, -14 + t * 2, 5, 4, skinMid);
    P(-14, -14 + t * 2, 5, 1, skin);
    P(9, -14 + (1 - t) * 2, 5, 4, skinDark);
    P(9, -14 + (1 - t) * 2, 5, 1, skinMid);
  } else {
    const tap = (sitting && look.habit === 'tap' && sp.idleFor > 3
                 && Math.floor(V.t * 5 + sp.phase * 3) % 2) ? -1 : 0;
    P(-16, armY + 13, 4, 4, skinMid);
    P(-16, armY + 13, 4, 1, skin);
    P(12, armY + 13 + tap, 4, 4, skinDark);
  }

  /* ---- head ---- */
  // The silhouette is cut at all four corners. Two pixels of chamfer is the
  // whole difference between a head and a box, and it is why every good sprite
  // at this size does it.
  const hy = -55;
  P(-9, hy - 4, 18, 1, OUTLINE);
  P(-11, hy - 3, 22, 1, OUTLINE);
  P(-12, hy - 2, 24, 20, OUTLINE);
  P(-11, hy + 18, 22, 1, OUTLINE);
  P(-9, hy + 19, 18, 1, OUTLINE);

  P(-8, hy, 16, 1, skin);                                // chamfered crown
  P(-9, hy + 1, 18, 1, skin);
  P(-10, hy + 2, 20, 14, skin);                          // face
  P(-9, hy + 16, 18, 1, skin);
  P(-8, hy + 17, 16, 1, skin);

  // Form shading. Light from upper-left, so: bright brow on the left, the
  // right cheek falls away, and a cool bounce runs up the far edge.
  P(-8, hy, 12, 1, skinLit);
  P(-9, hy + 1, 11, 1, skinLit);
  P(-10, hy + 2, 4, 9, skinLit);                         // lit temple + cheek
  P(6, hy + 2, 4, 13, skinMid);                          // shaded cheek
  P(8, hy + 3, 2, 12, skinDark);
  P(9, hy + 5, 1, 9, skinBounce);                        // bounce off the wall
  P(-6, hy + 15, 13, 2, skinMid);                        // under the jaw
  P(-5, hy + 17, 11, 1, skinDark);
  P(-10, hy + 12, 3, 4, skinMid);                        // jaw turns away

  // Only the ear on the viewer's side of a turned head exists. Drawing both
  // put a spare ear in the middle of the cheek whenever someone looked sideways.
  if (facing !== 'left') {
    P(-12, hy + 6, 2, 6, skin);
    P(-12, hy + 8, 2, 3, skinMid);
  }
  if (facing !== 'right') {
    P(10, hy + 6, 2, 6, skinMid);
    P(10, hy + 8, 2, 3, skinDark);
  }

  P(-4, hy + 18, 8, 5, skinMid);                         // neck
  P(-4, hy + 18, 8, 2, skinDark);                        // shadow the head casts
  P(-4, hy + 18, 2, 5, skinMid);
  P(2, hy + 18, 2, 5, skinDark);

  if (facing === 'up') {
    paintHair(P, look, hy, 'back');                      // back of the head
  } else {
    paintFace(P, look, expressionFor(r, sp), sp, hy, skin, skinDark, facing);
    paintHair(P, look, hy, facing);
  }

  /* ---- what they take their break with ---- */
  if (sp.prop === 'coffee' || sp.prop === 'tea') {
    const brew = sp.prop === 'tea' ? '#9c6b3a' : '#5f3d24';
    P(13, -30, 9, 10, '#efe9dd');                        // mug
    P(13, -30, 3, 10, '#ffffff');
    P(19, -30, 3, 10, '#cdc5b4');
    P(13, -30, 9, 1, '#ffffff');
    P(13, -21, 9, 1, '#b3aa98');
    P(15, -28, 5, 2, brew);
    P(22, -27, 2, 5, '#d8d2c4');                         // handle
    const st = Math.floor(V.t * 1.8) % 3;                // steam
    P(16, -34 - st, 2, 3, `rgba(255,255,255,${0.30 - st * 0.08})`);
  } else if (sp.prop === 'smoke') {
    P(13, -31, 7, 2, '#efe9dd');                         // cigarette
    P(19, -31, 2, 2, '#e0663c');                         // the lit end
    P(19, -31, 2, 1, '#f0a05c');
    // Smoke drifts up and sideways rather than rising in a column.
    const t = V.t * 1.1 + sp.phase * 4;
    for (let i = 0; i < 4; i++) {
      const rise = ((t + i * 0.55) % 2.2);
      const a = 0.26 * (1 - rise / 2.2);
      if (a <= 0.01) continue;
      P(20 + Math.round(Math.sin(rise * 2.6 + i) * 2),
        -33 - Math.round(rise * 7), 2, 2, `rgba(226,222,214,${a.toFixed(3)})`);
    }
  } else if (sp.prop === 'snack') {
    P(13, -29, 8, 7, '#c8863f');                         // pastry
    P(13, -29, 8, 2, '#dda058');
    P(15, -27, 4, 2, '#8a5527');
    P(12, -22, 10, 2, '#e8e2d4');                        // napkin
  } else if (sp.prop === 'phone') {
    P(14, -31, 6, 10, '#26252f');
    P(15, -30, 4, 8, '#5f88c4');
    P(15, -30, 4, 2, Math.floor(V.t * 2) % 2 ? '#7aa6e0' : '#5f88c4');
  }
  if (look.acc === 'headphones' && facing !== 'up') {
    P(-15, hy + 4, 5, 11, '#252331');                    // cups
    P(10, hy + 4, 5, 11, '#1d1b27');
    P(-15, hy + 4, 1, 11, '#3d3a4c');
    P(-13, hy + 6, 2, 6, '#4a4759');
    P(-10, hy - 5, 20, 3, '#252331');                    // band
    P(-10, hy - 5, 12, 1, '#454256');
  }
}

/* Hair. The single most common giveaway of amateur pixel art is hair painted
 * as one flat colour, so this builds it the way a painter would: a base, a
 * shadow where it turns away, a highlight band arcing over the skull toward
 * the light, and a cast shadow dropped onto the forehead beneath the fringe. */
function paintHair(P, look, hy, dir) {
  const h = look.hair;
  const lit = shade(h, 46);          // catches the lamp
  const mid = shade(h, 12);
  const dark = shade(h, -34);        // turns away
  const deep = shade(h, -58);
  const back = dir === 'back';
  const faceShadow = shade(look.skinHex, -34);

  if (look.style === 'bald') {
    P(-8, hy, 16, 1, shade(look.skinHex, 34));
    P(-9, hy + 1, 18, 1, shade(look.skinHex, 26));
    P(-10, hy + 2, 20, 2, shade(look.skinHex, 16));
    P(-7, hy, 7, 1, shade(look.skinHex, 52));            // a shine, unkindly
    P(-8, hy + 1, 5, 1, shade(look.skinHex, 44));
    if (!back) P(3, hy + 1, 6, 3, shade(look.skinHex, -18));
    return;
  }

  /* ---- skull cap, following the chamfered silhouette ---- */
  P(-7, hy - 4, 14, 1, mid);
  P(-9, hy - 3, 18, 1, h);
  P(-11, hy - 2, 22, 3, h);
  P(-11, hy + 1, 22, 3, h);
  P(-11, hy - 2, 3, 7, mid);                             // left edge
  P(8, hy - 2, 3, 7, dark);                              // right edge turns away
  P(10, hy, 1, 5, deep);

  /* ---- highlight band: an arc, not a stripe ---- */
  P(-7, hy - 3, 8, 1, lit);
  P(-9, hy - 2, 5, 2, lit);
  P(-4, hy - 2, 7, 1, lit);
  P(-10, hy, 3, 3, mid);
  P(-6, hy - 1, 4, 1, shade(h, 66));                     // specular pop
  P(1, hy - 3, 4, 1, mid);

  if (back) {                                            // whole back of the head
    P(-11, hy - 2, 22, 20, h);
    P(-11, hy - 2, 3, 20, mid);
    P(8, hy - 2, 3, 20, dark);
    P(-7, hy - 3, 8, 1, lit);
    P(-9, hy - 2, 5, 2, lit);
    P(-5, hy + 4, 10, 10, dark);                         // nape falls into shadow
    P(-4, hy + 12, 8, 5, deep);
  } else {
    // The fringe drops a shadow on the forehead. One pixel, sitting on bare
    // skin above the brow line - any lower and it paints over the eyebrows.
    P(-10, hy + 4, 20, 1, faceShadow);
    P(-10, hy + 4, 5, 1, shade(look.skinHex, -18));
  }

  switch (look.style) {
    case 'short':
      P(-11, hy + 4, 4, 5, h); P(7, hy + 4, 4, 5, dark);
      P(-11, hy + 4, 2, 4, mid); break;
    case 'messy':
      P(-11, hy - 6, 7, 4, h); P(-3, hy - 8, 6, 6, h); P(4, hy - 6, 6, 4, h);
      P(-10, hy - 5, 4, 2, lit); P(-2, hy - 7, 3, 2, lit);
      P(5, hy - 5, 3, 2, mid);
      P(-11, hy + 4, 4, 7, h); P(7, hy + 4, 4, 5, dark); break;
    case 'ponytail': {
      P(-11, hy + 4, 3, 5, mid); P(8, hy + 4, 3, 5, dark);
      const tx = back ? -4 : 10;
      P(tx, hy + 3, 7, 15, h);                           // the tail
      P(tx, hy + 3, 2, 15, mid);
      P(tx + 5, hy + 4, 2, 14, deep);
      P(tx + 1, hy + 5, 2, 6, lit);
      P(tx + 1, hy + 15, 5, 3, deep);
      P(tx - 1, hy + 2, 7, 2, dark);                     // the band
      break;
    }
    case 'bun':
      P(-5, hy - 10, 11, 7, h);
      P(-5, hy - 10, 4, 7, mid);
      P(2, hy - 9, 3, 6, dark);
      P(-4, hy - 9, 4, 2, lit);
      P(-6, hy - 6, 2, 3, dark);
      P(-11, hy + 4, 2, 4, mid); P(9, hy + 4, 2, 4, dark); break;
    case 'long':
      P(-13, hy + 1, 4, 18, h); P(9, hy + 1, 4, 18, dark);
      P(-13, hy + 1, 2, 13, mid);                        // lit curtain
      P(-13, hy + 3, 1, 7, lit);
      P(11, hy + 4, 2, 12, deep);
      P(-13, hy + 17, 4, 3, deep); P(9, hy + 17, 4, 3, deep);
      P(-11, hy + 4, 3, 9, h); break;
    case 'curls':
      P(-13, hy, 4, 7, h); P(9, hy, 4, 7, dark);
      P(-10, hy - 6, 7, 4, h); P(2, hy - 6, 7, 4, h);
      P(-9, hy - 5, 3, 2, lit); P(3, hy - 5, 3, 2, mid);
      P(-13, hy + 4, 3, 4, mid); P(10, hy + 4, 3, 4, deep);
      P(-6, hy - 7, 4, 2, lit); break;
    case 'cap': {
      const cb = shade(h, -30), cl = shade(h, 20), cd = shade(h, -56);
      P(-11, hy - 6, 22, 1, cl);                         // crown
      P(-12, hy - 5, 24, 7, cb);
      P(-12, hy - 5, 4, 7, cl);
      P(8, hy - 5, 4, 7, cd);
      P(-2, hy - 5, 4, 7, shade(h, -14));                // centre panel
      P(-9, hy - 4, 7, 1, shade(h, 44));                 // stitch highlight
      P(-13, hy + 2, 20, 4, cd);                         // brim
      P(-13, hy + 2, 20, 1, shade(h, -22));
      P(-13, hy + 5, 20, 1, shade(h, -72));
      P(-1, hy - 9, 3, 3, cb); break;                    // button
    }
  }
}

/* The face. Every expression is brows + eyes + mouth; the rest is casting. */
function paintFace(P, look, expr, sp, hy, skin, dark, facing) {
  const profile = facing === 'left' || facing === 'right';
  const flip = facing === 'left' ? -1 : 1;

  // Blink: brief, and out of phase per person so the room never blinks at once.
  const cycle = (V.t * 0.55 + sp.phase * 9) % 4.6;
  const blinking = cycle < 0.13 && expr !== 'happy' && expr !== 'pleased';

  const browY = hy + 6, eyeY = hy + 10, noseY = hy + 12, mouthY = hy + 16;
  // Brows are the hair's own colour, not a darkened version of it. Darker than
  // the hair and they merge with the eyes into one band across the face.
  const brow = shade(look.hair, 8);
  const mouth = shade(skin, -62);        // warm, not near-black

  if (profile) {
    // A turned head is not the front view with one eye deleted. The whole face
    // shifts toward the direction of travel, the far cheek becomes jaw, and
    // the nose is a small step in the silhouette rather than a spur.
    const F = flip;                                      // +1 right, -1 left
    const edge = F > 0 ? 10 : -12;                       // front of the face
    const back = F > 0 ? -10 : 8;                        // back of the skull

    P(back, hy + 2, 2, 14, dark);                        // back of head recedes
    P(F > 0 ? 7 : -9, hy + 3, 2, 12, shade(skin, -8));   // cheek plane

    // Nose: two pixels of step, with the nostril shadow under it.
    P(edge, noseY - 2, 2, 3, skin);
    P(edge, noseY + 1, 2, 1, dark);
    P(F > 0 ? 9 : -10, noseY - 3, 1, 5, shade(skin, 14));
    // Brow ridge and chin are what actually read as a profile.
    P(F > 0 ? 8 : -10, browY, 3, 2, shade(skin, 12));
    P(F > 0 ? 6 : -9, hy + 16, 4, 2, dark);              // chin

    const ex = F > 0 ? 3 : -7;                           // eye, pushed forward
    if (blinking || expr === 'happy' || expr === 'pleased') {
      P(ex, eyeY + 1, 4, 1, INK);
    } else {
      const dy = expr === 'think' ? -1 : 0;
      P(ex, eyeY + dy, 4, 3, INK);
      P(ex + (F > 0 ? 3 : 0), eyeY + dy, 1, 1, 'rgba(255,255,255,.55)');
    }
    const bx = F > 0 ? 2 : -7;
    P(bx, browY + (expr === 'focus' || expr === 'stern' || expr === 'cross' ? 2 : 1),
      5, 1, brow);

    // Mouth sits between the nose and the chin, not on the centre line.
    P(F > 0 ? 4 : -7, mouthY, 3, 1, mouth);
    paintFacial(P, look, hy, skin);
    return;
  }

  const L = -7, R = 3;                                   // eye columns (4 wide)

  /* ---- eyes ----
   * Dark eyes with a catchlight, not white eyeballs with a pupil floating in
   * them. At 4 pixels across, a visible sclera reads as a fixed staring gaze -
   * it was the single thing making the whole floor look haunted. Gaze is
   * suggested by shifting the whole eye a pixel instead. */
  const eye = (x, style) => {
    const spark = 'rgba(255,255,255,.55)';
    switch (style) {
      case 'shut':
        P(x, eyeY + 1, 4, 1, INK); break;
      case 'narrow':                                     // locked on
        P(x, eyeY + 1, 4, 2, INK);
        P(x + 3, eyeY + 1, 1, 1, spark); break;
      case 'up':                                         // thinking, gaze raised
        P(x, eyeY - 1, 4, 3, INK);
        P(x + 3, eyeY - 1, 1, 1, spark); break;
      case 'wide':
        P(x - 1, eyeY - 1, 5, 5, INK);
        P(x + 2, eyeY, 2, 2, spark); break;
      case 'down':                                       // downcast
        P(x, eyeY + 1, 4, 2, INK); break;
      case 'half':
        P(x, eyeY + 1, 4, 2, INK);
        P(x, eyeY, 4, 1, shade(skin, -18)); break;
      case 'arch':                                       // closed and curved up
        P(x, eyeY + 1, 1, 1, INK); P(x + 1, eyeY, 2, 1, INK);
        P(x + 3, eyeY + 1, 1, 1, INK); break;
      default:
        P(x, eyeY, 4, 1, INK);                           // lash line, darkest
        P(x, eyeY + 1, 4, 2, IRIS);                      // iris reads lighter
        P(x + 1, eyeY + 1, 2, 1, INK);                   // pupil
        P(x + 3, eyeY + 1, 1, 1, spark);
    }
  };

  const style = blinking ? 'shut'
    : expr === 'focus' ? 'narrow'
    : expr === 'think' ? 'up'
    : expr === 'worried' ? 'wide'
    : expr === 'sad' ? 'down'
    : expr === 'tired' ? 'half'
    : (expr === 'pleased' || expr === 'happy') ? 'arch'
    : expr === 'bored' ? 'half'
    : 'open';
  eye(L, style); eye(R, style);

  if (expr === 'pleased' || expr === 'happy') {          // rosy cheeks
    P(-10, eyeY + 4, 4, 2, 'rgba(214,109,94,.38)');
    P(6, eyeY + 4, 4, 2, 'rgba(214,109,94,.38)');
  }
  if (expr === 'worried') {                              // a bead of sweat
    P(9, hy + 3, 2, 4, 'rgba(150,205,235,.7)');
    P(9, hy + 6, 2, 2, 'rgba(190,225,245,.8)');
  }

  /* ---- brows: most of the emotion lives here, but thin. Two rows of dark
   * this close above the eyes fuses into a mask across the whole face. ---- */
  switch (expr) {
    case 'focus': case 'stern':
      P(L, browY + 2, 4, 2, brow); P(R, browY + 2, 4, 2, brow); break;
    case 'cross':                                        // driven inward, down
      P(L, browY + 1, 2, 1, brow); P(L + 1, browY + 2, 3, 2, brow);
      P(R, browY + 2, 3, 2, brow); P(R + 2, browY + 1, 2, 1, brow); break;
    case 'worried': case 'sad':                          // inner ends raised
      P(L, browY + 2, 2, 1, brow); P(L + 1, browY, 3, 1, brow);
      P(R, browY, 3, 1, brow); P(R + 2, browY + 2, 2, 1, brow); break;
    case 'think':
      P(L, browY + 1, 4, 1, brow); P(R, browY - 1, 4, 2, brow); break;
    case 'tired': case 'bored':
      P(L, browY + 2, 4, 1, brow); P(R, browY + 2, 4, 1, brow); break;
    default:
      P(L, browY + 1, 4, 1, brow); P(R, browY + 1, 4, 1, brow);
  }

  /* ---- nose: one pixel of shadow. Any more and it becomes a snout. ---- */
  P(0, noseY + 1, 2, 1, shade(skin, -26));

  /* ---- mouth: warm, small, and slightly upturned at rest. A long flat dark
   * line at this size reads as a grimace on every face in the room. ---- */
  switch (expr) {
    case 'pleased': case 'happy':                        // open smile
      P(-3, mouthY - 1, 1, 1, mouth); P(-2, mouthY, 4, 2, mouth);
      P(2, mouthY - 1, 1, 1, mouth); break;
    case 'sad':                                          // corners down
      P(-3, mouthY + 1, 1, 1, mouth); P(-2, mouthY, 4, 1, mouth);
      P(2, mouthY + 1, 1, 1, mouth); break;
    case 'worried':                                      // small open O
      P(-1, mouthY, 3, 3, '#6b3a3c');
      P(-1, mouthY, 3, 1, mouth); break;
    case 'focus':
      P(-2, mouthY, 4, 1, mouth); break;                 // small, set
    case 'think':
      P(0, mouthY, 3, 1, mouth); break;                  // pursed, off-centre
    case 'cross': case 'stern':
      P(-4, mouthY, 8, 1, mouth); break;                 // flat and long
    case 'bored':
      P(-3, mouthY, 5, 1, mouth); break;
    case 'tired': {                                      // the occasional yawn
      const yawn = Math.floor(V.t * 0.4 + sp.phase * 5) % 6 === 0;
      if (yawn) {
        P(-2, mouthY - 1, 5, 5, '#6b3a3c');
        P(-2, mouthY - 1, 5, 1, mouth);
      } else P(-2, mouthY, 4, 1, mouth);
      break;
    }
    default:                                             // resting, faintly warm
      P(-3, mouthY, 1, 1, mouth); P(-2, mouthY + 1, 4, 1, mouth);
      P(2, mouthY, 1, 1, mouth);
  }

  paintFacial(P, look, hy, skin);

  if (look.acc === 'glasses') {
    // Light frames. A full heavy rim reads as a bandit mask and swallows the
    // eyes, which are the whole point of drawing a face.
    // Thin rails only. A 2px top rail sits right under the brow and the two
    // merge into one heavy band across the face.
    const gl = 'rgba(58,50,72,.70)';
    P(L - 2, eyeY - 1, 8, 6, 'rgba(196,220,238,.10)');   // lenses
    P(R - 2, eyeY - 1, 8, 6, 'rgba(196,220,238,.10)');
    P(L - 1, eyeY - 1, 4, 1, 'rgba(255,255,255,.18)');   // glint
    P(L - 2, eyeY - 1, 8, 1, gl);                        // top rails
    P(R - 2, eyeY - 1, 8, 1, gl);
    P(L - 2, eyeY + 4, 8, 1, 'rgba(58,50,72,.40)');      // faint lower rim
    P(R - 2, eyeY + 4, 8, 1, 'rgba(58,50,72,.40)');
    P(L - 2, eyeY - 1, 1, 6, gl); P(R + 5, eyeY - 1, 1, 6, gl);
    P(L + 6, eyeY, 2, 1, gl);                            // bridge
    P(-12, eyeY, 2, 1, gl); P(10, eyeY, 2, 1, gl);       // temples
  }
  if (look.acc === 'earrings') {
    P(-12, eyeY + 6, 2, 3, '#e8c765'); P(10, eyeY + 6, 2, 3, '#e8c765');
    P(-12, eyeY + 6, 2, 1, '#fce9a8'); P(10, eyeY + 6, 2, 1, '#fce9a8');
  }
}

function paintFacial(P, look, hy, skin) {
  const h = shade(look.hair, -16), lit = shade(look.hair, 6);
  if (look.facial === 'beard') {
    P(-9, hy + 16, 18, 5, h);                            // jaw
    P(-10, hy + 13, 2, 6, h); P(8, hy + 13, 2, 6, h);    // sideburns
    P(-4, hy + 13, 8, 2, h);                             // moustache
    P(-2, hy + 16, 4, 1, shade(skin, -62));              // mouth reads through
    P(-7, hy + 20, 14, 1, lit);
  } else if (look.facial === 'moustache') {
    P(-5, hy + 13, 10, 2, h);
    P(-5, hy + 13, 4, 1, lit);
  } else if (look.facial === 'stubble') {
    P(-7, hy + 15, 14, 3, 'rgba(58,46,42,.18)');
    P(-9, hy + 12, 2, 5, 'rgba(58,46,42,.16)');
    P(7, hy + 12, 2, 5, 'rgba(58,46,42,.16)');
  }
}

function paintFurniture(f) {
  const [x0, y0] = f.tiles[0];
  const x = x0 * T, y = y0 * T;
  switch (f.kind) {
    case 'coffee': {
      px(x, y + 2, T * 2, T - 2, '#43424e');                 // counter
      px(x, y + 2, T * 2, 4, '#5a5967');
      px(x, y + T - 4, T * 2, 4, '#31303a');
      px(x + 4, y + 8, 22, 18, '#22212a');                   // machine body
      px(x + 4, y + 8, 22, 3, '#3a3945');
      px(x + 7, y + 12, 16, 8, '#151419');                   // window
      px(x + 8, y + 13, 14, 6, shade('#8b5a2b', 34));
      px(x + 8, y + 13, 14, 1, 'rgba(255,210,150,.4)');
      px(x + 12, y + 21, 6, 4, '#6f6e7c');                   // spout
      px(x + 11, y + 25, 8, 7, '#d9d5cb');                   // pot
      px(x + 11, y + 25, 8, 1, '#f0ece2');
      px(x + 12, y + 29, 6, 2, '#7a5535');
      const st = Math.floor(V.t * 1.6) % 3;                  // steam
      px(x + 14, y + 3 - st * 2, 3, 4, `rgba(255,255,255,${0.26 - st * 0.07})`);
      px(x + T + 6, y + 10, 20, 16, '#6f6e7c');              // cup stack
      px(x + T + 6, y + 10, 20, 2, '#8e8d9b');
      px(x + T + 7, y + 15, 18, 2, '#8e8d9b');
      px(x + T + 7, y + 20, 18, 2, '#8e8d9b');
      break;
    }
    case 'sofa':
      px(x + 2, y + 8, T * 3 - 4, T - 8, '#6d4548');         // base
      px(x, y + 2, T * 3, 12, '#8e5f62');                    // backrest
      px(x, y + 2, T * 3, 3, '#a57578');
      px(x, y + 12, T * 3, 2, '#6d4548');
      for (let i = 0; i < 3; i++) {                          // cushions
        const cx2 = x + 4 + i * T;
        px(cx2, y + 14, T - 6, 14, '#96686b');
        px(cx2, y + 14, T - 6, 2, '#ab7c7f');
        px(cx2, y + 26, T - 6, 2, '#6d4548');
        px(cx2 + 3, y + 17, T - 12, 1, 'rgba(255,255,255,.10)');
      }
      px(x, y + 10, 6, 18, '#7a4f52');                       // arms
      px(x + T * 3 - 6, y + 10, 6, 18, '#7a4f52');
      px(x, y + 10, 6, 2, '#8e5f62');
      px(x + T * 3 - 6, y + 10, 6, 2, '#8e5f62');
      break;
    case 'table':
      px(x, y + 8, T * 2, T - 12, '#6b4f34');
      px(x, y + 8, T * 2, 4, '#8f6e4a');
      px(x, y + 11, T * 2, 1, 'rgba(255,238,205,.12)');
      px(x + 4, y + T - 4, 5, 8, '#4a3524');
      px(x + T * 2 - 9, y + T - 4, 5, 8, '#4a3524');
      px(x + 9, y + 14, 15, 8, '#cfcabb');                   // magazine
      px(x + 9, y + 14, 15, 1, '#e4dfd2');
      px(x + 11, y + 17, 10, 1, 'rgba(90,80,70,.45)');
      px(x + 11, y + 19, 7, 1, 'rgba(90,80,70,.35)');
      px(x + T + 8, y + 12, 8, 10, '#d9d5cb');               // someone's mug
      px(x + T + 8, y + 12, 8, 2, '#f0ece2');
      break;
    case 'vending':
      px(x, y - T + 4, T, T * 2 - 4, '#3c5a54');
      px(x, y - T + 4, T, 3, '#527670');
      px(x + 4, y - T + 10, T - 10, T + 6, '#0e1a18');       // glass
      px(x + 4, y - T + 10, T - 10, T + 6, 'rgba(127,194,178,.22)');
      for (let row = 0; row < 4; row++)                      // rows of snacks
        for (let col = 0; col < 3; col++)
          px(x + 6 + col * 7, y - T + 13 + row * 9, 5, 6,
             ['#e8e3d8', '#e8b34f', '#d4695e', '#7fc2b2'][(row + col) % 4]);
      px(x + 5, y - T + 8, T - 12, 2, 'rgba(255,255,255,.20)');  // reflection
      px(x + 6, y + T - 12, T - 12, 7, '#22322f');           // tray
      px(x + 6, y + T - 12, T - 12, 1, '#3d5450');
      px(x + T - 8, y - T + 12, 4, 10, '#e8b34f');           // keypad
      break;
    case 'plant':
      px(x + 8, y + 18, 16, 12, '#8a5a3c');                  // pot
      px(x + 8, y + 18, 16, 3, '#a67349');
      px(x + 8, y + 27, 16, 3, '#6d452c');
      px(x + 10, y + 15, 12, 4, '#6f4830');                  // soil
      px(x + 13, y - 4, 7, 20, '#4e7a44');                   // fronds
      px(x + 4, y + 2, 8, 13, '#3f6838');
      px(x + 21, y + 1, 8, 14, '#3f6838');
      px(x + 8, y - 6, 6, 9, '#5b8a4f');
      px(x + 19, y - 8, 6, 11, '#5b8a4f');
      px(x + 14, y - 10, 5, 8, '#68a05a');
      px(x + 14, y - 4, 2, 14, 'rgba(255,255,255,.10)');
      break;
    case 'cooler':
      px(x + 6, y + 10, 20, 20, '#cfd8dc');                  // stand
      px(x + 6, y + 10, 20, 2, '#e8eef0');
      px(x + 6, y + 26, 20, 4, '#9aa5aa');
      px(x + 8, y - 10, 16, 20, '#79b9d6');                  // bottle
      px(x + 10, y - 8, 12, 16, 'rgba(198,234,247,.55)');
      px(x + 10, y - 8 + (Math.floor(V.t) % 2) * 2, 12, 2, 'rgba(255,255,255,.45)');
      px(x + 11, y - 4, 3, 9, 'rgba(255,255,255,.30)');
      px(x + 10, y + 16, 12, 4, '#8b98a0');                  // tap
      px(x + 14, y + 20, 4, 4, '#6d7880');
      break;
    case 'printer':
      px(x + 2, y + 8, 28, 20, '#4a4956');
      px(x + 2, y + 8, 28, 3, '#5d5c6b');
      px(x + 6, y + 2, 20, 7, '#5d5c6b');                    // paper tray
      px(x + 8, y, 16, 4, '#e8e3d8');
      px(x + 6, y + 18, 20, 8, '#e8e3d8');                   // output
      px(x + 6, y + 18, 20, 1, '#f6f2e8');
      px(x + 9, y + 21, 14, 1, 'rgba(90,80,70,.4)');
      px(x + 9, y + 23, 10, 1, 'rgba(90,80,70,.3)');
      px(x + 24, y + 12, 4, 4,                               // status lamp
         Math.floor(V.t * 1.5) % 2 ? '#6fae63' : '#3d5c39');
      break;
    case 'cabinet':
      px(x + 2, y - T + 4, 28, T * 2 - 6, '#5a5462');
      px(x + 2, y - T + 4, 28, 3, '#6d6676');
      px(x + 2, y - T + 4, 3, T * 2 - 6, '#665f70');
      for (let i = 0; i < 3; i++) {                          // drawers
        const dy = y - T + 10 + i * 18;
        px(x + 5, dy, 22, 15, '#4e4956');
        px(x + 5, dy, 22, 1, '#615b6c');
        px(x + 5, dy + 14, 22, 1, '#3b3743');
        px(x + 12, dy + 6, 8, 3, '#8d8699');                 // handle
      }
      px(x + 6, y - T - 3, 20, 7, '#7d6a45');                // box on top
      px(x + 6, y - T - 3, 20, 2, '#98835a');
      break;
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
  // Kept small on purpose: a bubble competes with the floor for space, and at
  // 12.5px with generous padding two people talking hid a third of the room.
  ctx.font = '11px system-ui';
  const lines = wrap(text, 168, 4);
  const lh = 13;
  const w = Math.max(28, ...lines.map(l => ctx.measureText(l).width)) + 12;
  const h = lines.length * lh + 8;
  const bx = Math.round(x - w / 2), by = Math.round(y - h);

  ctx.fillStyle = 'rgba(16,15,22,.94)';
  ctx.fillRect(bx, by, w, h);
  ctx.fillStyle = color;
  ctx.fillRect(bx, by, w, 2);
  ctx.fillRect(Math.round(x) - 3, by + h, 6, 4);

  ctx.fillStyle = '#ece3d6';
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  lines.forEach((l, i) => ctx.fillText(l, x, by + 4 + i * lh));
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

/* ---------------------------------------------------------------- colour --
 *
 * Shading by adding or subtracting brightness is what makes flat pixel art
 * look like flat pixel art: every ramp drifts toward grey and the whole scene
 * reads as tinted rather than lit. Real ramps shift hue as well as value -
 * light in this room is a warm lamp, so highlights bend toward amber and lose
 * saturation, while shadows fall into a cool ambient blue and gain it.
 *
 * Everything on the floor is drawn through this one function, so the hue shift
 * is what gives the whole office a consistent light source.
 */

const LIGHT_HUE = 0.09;      // warm lamp
const SHADOW_HUE = 0.62;     // cool ambient bounce
const _shadeCache = new Map();

function hexToHsl(hex) {
  const n = parseInt(hex.slice(1), 16);
  const r = ((n >> 16) & 255) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  const l = (mx + mn) / 2;
  let h = 0, sat = 0;
  if (mx !== mn) {
    const d = mx - mn;
    sat = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    h = mx === r ? (g - b) / d + (g < b ? 6 : 0)
      : mx === g ? (b - r) / d + 2
      : (r - g) / d + 4;
    h /= 6;
  }
  return [h, sat, l];
}

function hslToHex(h, s, l) {
  h = ((h % 1) + 1) % 1;
  let r, g, b;
  if (s === 0) { r = g = b = l; }
  else {
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    const hue = (t) => {
      t = ((t % 1) + 1) % 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };
    r = hue(h + 1 / 3); g = hue(h); b = hue(h - 1 / 3);
  }
  const to = (v) => Math.round(Math.max(0, Math.min(1, v)) * 255);
  return `rgb(${to(r)},${to(g)},${to(b)})`;
}

/** Blend a hue toward a target the short way round the wheel. */
function towardHue(h, target, amount) {
  let d = target - h;
  if (d > 0.5) d -= 1; else if (d < -0.5) d += 1;
  return h + d * amount;
}

function shade(hex, amt) {
  const key = hex + '|' + amt;
  const hit = _shadeCache.get(key);
  if (hit) return hit;

  const t = Math.max(-1, Math.min(1, amt / 80));
  let [h, sat, l] = hexToHsl(hex);
  if (t < 0) {
    const k = -t;                          // into shadow: cooler, richer
    h = towardHue(h, SHADOW_HUE, 0.20 * k);
    sat = Math.min(1, sat + 0.26 * k * (1 - sat * 0.4));
    l = Math.max(0.02, l - 0.40 * k * (0.45 + l * 0.7));
  } else {                                 // into light: warmer, paler
    h = towardHue(h, LIGHT_HUE, 0.17 * t);
    sat = Math.max(0, sat * (1 - 0.24 * t));
    l = Math.min(0.98, l + 0.34 * t * (1.05 - l));
  }
  const out = hslToHex(h, sat, l);
  if (_shadeCache.size < 4000) _shadeCache.set(key, out);
  return out;
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
  $('msgTo').onchange = updateComposerHint;
  $('budgetStat').onclick = openUsage;
  $('usageRefresh').onclick = loadUsage;
  $('usageClose').onclick = closeUsage;
  $('usageModal').addEventListener('click', (e) => {
    if (e.target === $('usageModal')) closeUsage();
  });
  $('filesBtn').onclick = openFiles;
  $('filesRefresh').onclick = loadFiles;
  $('filesClose').onclick = () => $('filesModal').classList.add('hidden');
  $('filesModal').addEventListener('click', (e) => {
    if (e.target === $('filesModal')) $('filesModal').classList.add('hidden');
  });
  $('staffBtn').onclick = openStaff;
  $('staffClose').onclick = closeStaff;
  $('staffExport').onclick = exportOffice;
  $('staffImport').onclick = () => $('staffFile').click();
  $('staffFile').onchange = importOffice;
  $('staffModal').addEventListener('click', (e) => {
    if (e.target === $('staffModal')) closeStaff();
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
      const mood = MOODS[expressionFor(r, sp)];
      tip.innerHTML = `<b>${r.emoji} ${r.name} — ${r.title}</b>` +
        `${a.status || 'idle'}${where}${a.detail ? ' · ' + escapeHtml(a.detail) : ''}` +
        (mood ? `<i class="mood">${mood}</i>` : '');
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

/** Keeps the To picker in step with the roster, preserving the selection. */
function renderRecipients() {
  const sel = $('msgTo');
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = '';
  const manager = S.roster.find(r => r.manager);
  const add = (value, label) => {
    const o = document.createElement('option');
    o.value = value; o.textContent = label;
    sel.appendChild(o);
  };
  if (manager) add(manager.id, `${manager.emoji} ${manager.name} · delegates`);
  for (const r of S.roster) {
    if (r.manager) continue;
    add(r.id, `${r.emoji} ${r.name} · ${r.title}`);
  }
  sel.value = S.roster.some(r => r.id === keep) ? keep
            : (manager ? manager.id : (S.roster[0] || {}).id || '');
  updateComposerHint();
}

function updateComposerHint() {
  const sel = $('msgTo'), input = $('msg');
  if (!sel || !input) return;
  const who = S.byId[sel.value];
  if (!who) return;
  input.placeholder = who.manager
    ? `Ask ${who.name} for something — he'll delegate it…`
    : `Give ${who.name} a job directly…`;
}

async function send() {
  const input = $('msg');
  const text = input.value.trim();
  if (!text) return;
  const to = ($('msgTo') || {}).value || '';
  input.value = '';
  $('send').disabled = true;
  try {
    await fetch('/api/message', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, to })
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

function niceTokens(n) {
  n = n || 0;
  if (n < 1000) return String(n);
  if (n < 1000000) return (n / 1000).toFixed(n < 10000 ? 1 : 0) + 'k';
  return (n / 1000000).toFixed(2) + 'M';
}

function renderSpend() {
  const session = S.spend.session || {};
  const day = S.spend.day || {};
  const tokens = session.tokens ?? 0;
  const usd = session.usd ?? 0;
  const hours = Math.round((S.spend.session_window_seconds || 18000) / 3600);
  const tokenCap = S.spend.token_budget || 0;
  const dayCap = S.spend.daily_budget || 0;

  $('spendTokens').textContent = niceTokens(tokens) + ' tok';
  $('spend').textContent = usd ? `$${usd.toFixed(3)}` : '$0.000';

  const fill = $('meterFill');
  const meter = fill && fill.parentElement;
  if (!fill) return;

  // A meter needs a denominator. There is no way to read the plan's real
  // limit, so the bar only appears against a ceiling you set yourself.
  const [used, cap] = tokenCap ? [tokens, tokenCap] : [day.usd ?? 0, dayCap];
  if (meter) meter.classList.toggle('hidden', !cap);
  if (cap) {
    const pct = Math.min(100, used / cap * 100);
    fill.style.width = pct.toFixed(1) + '%';
    fill.className = pct > 92 ? 'over' : pct > 70 ? 'near' : '';
  }

  const capLine = tokenCap
    ? `\n${niceTokens(tokens)} of ${niceTokens(tokenCap)} tokens (OFFICE_SESSION_TOKEN_BUDGET).`
    : dayCap ? `\n$${(day.usd ?? 0).toFixed(3)} of $${dayCap.toFixed(2)} today; the office pauses at the cap.`
    : '\nNo ceiling set. OFFICE_SESSION_TOKEN_BUDGET adds one.';
  $('budgetStat').title =
    `Last ${hours}h: ${tokens.toLocaleString()} tokens, $${usd.toFixed(4)}.`
    + `\nToday: ${(day.tokens ?? 0).toLocaleString()} tokens, $${(day.usd ?? 0).toFixed(4)}.`
    + capLine + '\nClick for the per-employee breakdown.';
}

/* ----------------------------------------------------------------- usage -- */

const USAGE = { open: false, window: 'session', data: null };

const WINDOW_LABELS = [
  ['session', 'Last 5h'], ['day', 'Today'], ['week', '7 days'], ['all', 'All time'],
];

async function openUsage() {
  USAGE.open = true;
  $('usageModal').classList.remove('hidden');
  await loadUsage();
}

function closeUsage() {
  USAGE.open = false;
  $('usageModal').classList.add('hidden');
}

async function loadUsage() {
  try {
    USAGE.data = await (await fetch('/api/usage?window=' + USAGE.window)).json();
  } catch {
    $('usageBody').innerHTML = '<p class="muted">could not read usage</p>';
    return;
  }
  renderUsage();
}

function renderUsage() {
  const d = USAGE.data;
  if (!d) return;

  const tabs = $('usageWindows');
  tabs.innerHTML = '';
  for (const [id, label] of WINDOW_LABELS) {
    const hours = Math.round((d.session_window_seconds || 18000) / 3600);
    const b = el('button', 'uTab' + (id === USAGE.window ? ' on' : ''),
                 id === 'session' ? `Last ${hours}h` : label);
    b.onclick = () => { USAGE.window = id; loadUsage(); };
    tabs.appendChild(b);
  }

  const body = $('usageBody');
  body.innerHTML = '';
  const t = d.totals || {};

  if (!t.turns) {
    body.innerHTML = '<p class="muted">Nothing recorded in this window yet.</p>';
    return;
  }

  /* ---- headline numbers ---- */
  const cards = el('div', 'uCards');
  const card = (label, value, sub) => {
    const c = el('div', 'uCard');
    c.appendChild(el('div', 'uCardLabel', label));
    c.appendChild(el('div', 'uCardValue', value));
    if (sub) c.appendChild(el('div', 'uCardSub', sub));
    return c;
  };
  const reads = d.cost_split.find(k => k.kind === 'cache_read') || { share: 0 };
  // Share of prompt tokens that came from cache. Near zero means the stable
  // prefix is too short to be cached at all - the persona alone often is.
  const promptTok = (t.input || 0) + (t.cache_read || 0) + (t.cache_write || 0);
  const hitPct = promptTok ? Math.round(100 * (t.cache_read || 0) / promptTok) : 0;
  cards.append(
    card('Tokens', niceTokens(t.tokens), t.tokens.toLocaleString() + ' exactly'),
    card('Cost', '$' + (t.cost || 0).toFixed(4),
         d.token_budget ? '' : 'notional on a subscription'),
    card('Turns', String(t.turns), 'model calls'),
    card('Cache hit', `${hitPct}%`,
         `${niceTokens(t.cache_read)} read back at a tenth of input price — `
         + `${reads.share}% of spend`),
  );
  body.appendChild(cards);

  /* ---- where the money went, which is not where the tokens went ----
   * Token volume and cost point in opposite directions: cache reads are
   * usually the biggest column of tokens and the smallest column of spend.
   * Showing volume alone made the cheapest thing look like the problem. */
  body.appendChild(el('h3', 'uHead', 'Where the cost went'));
  const KIND = {
    output: ['Output', 'the most expensive token there is'],
    cache_write: ['Cache written', 'first sight of a prompt, at 1.25x input'],
    cache_read: ['Cache read', 'a tenth of input price — this is the saving'],
    input: ['Fresh input', 'uncached prompt'],
  };
  const split = el('div', 'uSplit');
  for (const k of d.cost_split) {
    if (!k.tokens) continue;
    const [label, note] = KIND[k.kind] || [k.kind, ''];
    const row = el('div', 'uSplitRow' + (k.kind === 'cache_read' ? ' good' : ''));
    row.innerHTML =
      `<div class="uSplitHead"><span class="uWho">${label}</span>`
      + `<span class="uTok">${niceTokens(k.tokens)} tok</span>`
      + `<span class="uCost">$${k.cost.toFixed(5)}</span>`
      + `<span class="uShare">${k.share}%</span></div>`
      + `<div class="uBar"><i style="width:${k.share}%"></i></div>`
      + `<div class="uBreak">${note}</div>`;
    split.appendChild(row);
  }
  body.appendChild(split);
  body.appendChild(el('p', 'muted uNote',
    'Bars are share of cost, not share of tokens. Cache reads are cheap on '
    + 'purpose: without them that prompt would be billed as fresh input at ten '
    + 'times the rate. A large cache-read number is the caching working.'));

  /* ---- per employee: the answer to "who is spending this" ---- */
  body.appendChild(el('h3', 'uHead', 'By employee'));
  const max = Math.max(...d.by_agent.map(a => a.tokens), 1);
  const list = el('div', 'uRows');
  for (const a of d.by_agent) {
    const row = el('div', 'uRow');
    const head = el('div', 'uRowHead');
    head.innerHTML =
      `<span class="uWho" style="color:${a.color}">${a.emoji} ${escapeHtml(a.name)}</span>`
      + (a.departed ? '<span class="uGone">left</span>' : '')
      + `<span class="uTok">${niceTokens(a.tokens)}</span>`
      + `<span class="uCost">$${(a.cost || 0).toFixed(4)}</span>`;
    row.appendChild(head);

    const bar = el('div', 'uBar');
    const fill = el('i');
    fill.style.width = (a.tokens / max * 100).toFixed(1) + '%';
    fill.style.background = a.color;
    bar.appendChild(fill);
    row.appendChild(bar);

    row.appendChild(el('div', 'uBreak',
      `${a.turns} turn${a.turns === 1 ? '' : 's'}`
      + (a.tasks ? ` over ${a.tasks} task${a.tasks === 1 ? '' : 's'}` : '')
      + ` · $${a.cost_per_turn.toFixed(4)}/turn`
      + (a.model_id ? ` · ${a.model_id}` : '')
      + ` · in ${niceTokens(a.input)} · out ${niceTokens(a.output)}`
      + ` · cache r/w ${niceTokens(a.cache_read)}/${niceTokens(a.cache_write)}`));

    // Only shown when this person's own numbers justify it.
    for (const tip of (a.advice || [])) {
      const t = el('div', 'uTip', tip);
      row.appendChild(t);
    }
    list.appendChild(row);
  }
  body.appendChild(list);

  /* ---- per model ---- */
  body.appendChild(el('h3', 'uHead', 'By model'));
  const models = el('div', 'uRows');
  for (const m of d.by_model) {
    const row = el('div', 'uRow uRowTight');
    row.innerHTML =
      `<div class="uRowHead"><span class="uWho">${escapeHtml(m.model)}</span>`
      + `<span class="uTok">${niceTokens(m.tokens)}</span>`
      + `<span class="uCost">$${(m.cost || 0).toFixed(4)}</span></div>`
      + `<div class="uBreak">${m.turns} turn${m.turns === 1 ? '' : 's'}</div>`;
    models.appendChild(row);
  }
  body.appendChild(models);

  /* ---- the raw turns, newest first ---- */
  body.appendChild(el('h3', 'uHead', 'Recent turns'));
  const recent = el('div', 'uTurns');
  const byId = S.byId;
  for (const r of d.recent) {
    const who = byId[r.agent_id] || { emoji: '👤', name: r.agent_id, color: '' };
    const line = el('div', 'uTurn');
    line.innerHTML =
      `<span class="when">${ago(r.ts)}</span>`
      + `<span class="uWho" style="color:${who.color}">${who.emoji} ${escapeHtml(who.name)}</span>`
      + `<span class="uModel">${escapeHtml(r.model)}</span>`
      + `<span class="uTok">${niceTokens((r.input_tokens || 0) + (r.output_tokens || 0)
           + (r.cache_read || 0) + (r.cache_write || 0))}</span>`
      + `<span class="uCost">$${(r.cost_usd || 0).toFixed(4)}</span>`;
    recent.appendChild(line);
  }
  body.appendChild(recent);
}

function tickClock() {
  if (!S.startedAt) return;
  const s = Math.max(0, Math.floor(Date.now() / 1000 - S.startedAt));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  $('uptime').textContent = h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}

function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h';
  return Math.floor(s / 86400) + 'd';
}

function renderBoard() {
  const groups = [
    ['In progress', S.tasks.filter(t => t.status === 'running')],
    ['Queued', S.tasks.filter(t => t.status === 'queued')],
    ['Finished', S.tasks.filter(t => ['done', 'partial', 'failed'].includes(t.status)).slice(0, 14)],
  ];
  const board = $('board');
  board.innerHTML = '';

  for (const [name, items] of groups) {
    if (!items.length) continue;
    const h = document.createElement('h3');
    h.innerHTML = `${name}<span class="count">${items.length}</span>`;
    board.appendChild(h);

    for (const t of items) {
      const who = S.byId[t.assignee] || {};
      const card = document.createElement('div');
      card.className = 'card ' + t.status;
      card.style.setProperty('--who', who.color || 'var(--accent)');
      const when = t.finished_at || t.started_at || t.created_at;
      card.innerHTML =
        `<div class="cardTop">
           <span class="dotStatus ${t.status}"></span>
           <span class="cardTitle">${escapeHtml(t.title)}</span>
         </div>
         <div class="cardMeta">
           <span class="chip" style="color:${who.color || ''}">
             ${who.emoji || ''} ${escapeHtml(who.name || t.assignee)}</span>
           <span class="when">${ago(when)}</span>
         </div>`;

      // Results are often the thing you actually want to read, and a title
      // attribute hides them behind a hover you have to know about.
      // A partial task holds a real result that simply is not finished:
      // say so on the card, or a half answer reads as a whole one.
      if (t.status === 'partial') {
        const why = String(t.stop || 'cap').replace(/_/g, ' ');
        card.querySelector('.cardMeta').appendChild(
          el('span', 'cardStop', `stopped early: ${why}`));
      }
      const body = (t.status === 'failed' ? t.error : t.result) || t.brief || '';
      if (body) {
        const more = document.createElement('div');
        more.className = 'cardBody hidden';
        more.textContent = body;

        if ((t.status === 'done' || t.status === 'partial') && t.result) {
          const save = el('button', 'ghost cardSave', 'Save as file');
          const slug = (t.title || 'result').toLowerCase()
            .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 48);
          save.onclick = (e) => {
            e.stopPropagation();               // do not collapse the card
            saveText(`${slug || 'result'}.md`, t.result);
          };
          more.appendChild(save);
        }
        card.appendChild(more);
        card.classList.add('expandable');
        card.onclick = () => {
          more.classList.toggle('hidden');
          card.classList.toggle('open');
        };
      }
      board.appendChild(card);
    }
  }

  if (!board.children.length) {
    board.innerHTML =
      '<p class="muted">Nothing on the board yet. Ask Miles for something, or '
      + 'send a job straight to someone with the <b>To</b> picker.</p>';
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
    el.className = 'approval' + (a.kind === 'hire' ? ' hire' : '');
    const isQuestion = a.kind === 'question';
    const isHire = a.kind === 'hire';
    const lead = isQuestion ? 'is asking you'
      : isHire ? 'wants to hire someone' : 'wants to run';
    el.innerHTML = `
      <div class="from">${who.emoji || ''} <b>${escapeHtml(who.name || a.agent_id)}</b>
        ${lead}${a.detail && !isHire ? ' · ' + escapeHtml(a.detail) : ''}</div>
      ${isHire ? '<p class="muted approvalNote">This adds a permanent member of '
        + 'staff. Read the persona — it is every instruction they will ever '
        + 'have.</p>' : ''}
      <code>${escapeHtml(a.action)}</code>
      ${isQuestion ? '<input placeholder="Your answer…">' : ''}
      <div class="actions"></div>`;
    const actions = el.querySelector('.actions');
    const input = el.querySelector('input');
    const yes = document.createElement('button');
    yes.className = 'btn-ok';
    yes.textContent = isQuestion ? 'Reply' : isHire ? 'Hire them' : 'Approve';
    yes.onclick = () => decide(a.id, true, input ? input.value : '');
    const no = document.createElement('button');
    no.className = 'btn-no';
    no.textContent = isQuestion ? 'Skip' : isHire ? 'Not this one' : 'Deny';
    no.onclick = () => decide(a.id, false, '');
    actions.append(yes, no);
    if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter') yes.click(); });
    box.appendChild(el);
  }
  if (list.length) $('modal').classList.remove('hidden');
}

/* ----------------------------------------------------------------- files -- */

function niceSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

/** Save text the browser already has, without a round trip to the daemon. */
function saveText(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown' }));
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function openFiles() {
  $('filesModal').classList.remove('hidden');
  await loadFiles();
}

async function loadFiles() {
  const box = $('filesList');
  box.innerHTML = '<p class="muted">loading…</p>';
  let files = [];
  try {
    files = (await (await fetch('/api/files')).json()).files || [];
  } catch {
    box.innerHTML = '<p class="muted">could not read the workspace</p>';
    return;
  }

  box.innerHTML = '';
  if (!files.length) {
    box.innerHTML =
      '<p class="muted">Nothing here yet. Only staff with the <b>Write</b> tool '
      + 'can save files — check the Staff panel if you expected something. '
      + 'Work that comes back as text instead lives on the task card, which has '
      + 'its own Save button.</p>';
    return;
  }

  for (const f of files) {
    const row = el('div', 'fileRow');
    const main = el('div', 'fileMain');
    main.appendChild(el('div', 'fileName', f.path));
    main.appendChild(el('div', 'fileMeta',
      `${niceSize(f.size)} · ${ago(f.modified)} ago`));
    row.appendChild(main);

    const actions = el('div', 'sActions');
    const open = el('a', 'ghost', 'Open');
    open.href = '/api/file/' + encodeURI(f.path);
    open.target = '_blank';
    open.rel = 'noopener';
    const dl = el('a', 'ghost', 'Download');
    dl.href = '/api/file/' + encodeURI(f.path);
    dl.download = f.name;
    actions.append(open, dl);
    row.appendChild(actions);
    box.appendChild(row);
  }
}

/* ----------------------------------------------------------------- setup -- */

const SETUP = { packs: [], chosen: null, touched: false };

async function openSetup() {
  const data = await (await fetch('/api/setup')).json();
  SETUP.packs = data.packs || [];
  SETUP.chosen = SETUP.chosen || (SETUP.packs[0] || {}).id || null;
  $('setupModal').classList.remove('hidden');
  renderSetup();
  // Typing your own answer must survive clicking between packs.
  $('setupPrincipal').oninput = () => { SETUP.touched = true; };
  $('setupGo').onclick = submitSetup;
}

function renderSetup() {
  const box = $('packs');
  box.innerHTML = '';
  for (const pack of SETUP.packs) {
    const card = el('div', 'pack' + (pack.id === SETUP.chosen ? ' on' : ''));
    card.appendChild(el('div', 'packName', pack.name));
    card.appendChild(el('div', 'packBlurb', pack.blurb));

    const row = el('div', 'packStaff');
    for (const p of pack.staff) {
      const chip = el('span', 'packChip', `${p.emoji} ${p.name}`);
      chip.style.borderColor = p.color;
      chip.title = p.title;
      row.appendChild(chip);
    }
    if (!pack.staff.length) row.appendChild(el('span', 'muted', 'nobody yet'));
    card.appendChild(row);

    card.onclick = () => {
      SETUP.chosen = pack.id;
      // Only overwrite the principal box while it is still the suggestion.
      if (!SETUP.touched) $('setupPrincipal').value = pack.principal;
      renderSetup();
    };
    box.appendChild(card);
  }
  const chosen = SETUP.packs.find(p => p.id === SETUP.chosen);
  if (chosen && !SETUP.touched) $('setupPrincipal').value = chosen.principal;
}

async function submitSetup() {
  const principal = $('setupPrincipal').value.trim();
  const err = $('setupErr');
  if (principal.length < 3) {
    err.textContent = 'Say who the office works for.';
    err.classList.remove('hidden');
    return;
  }
  $('setupGo').disabled = true;
  try {
    const res = await fetch('/api/setup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pack: SETUP.chosen, principal })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      err.textContent = data.error || 'that did not work';
      err.classList.remove('hidden');
      return;
    }
    $('setupModal').classList.add('hidden');
    await loadState();
    buildMap();
  } finally { $('setupGo').disabled = false; }
}

/* ----------------------------------------------------------------- staff -- */

const STAFF = { roster: [], catalogue: null, help: {}, editing: null, open: false };

async function applyRosterChange(ev) {
  await loadState();
  buildMap();                      // desks are walls; the floor plan moved
  if (STAFF.open) await loadRoster();
  pushFeed(ev);
}

async function openStaff() {
  STAFF.open = true;
  $('staffModal').classList.remove('hidden');
  await loadRoster();
}

function closeStaff() {
  STAFF.open = false;
  STAFF.editing = null;
  $('staffModal').classList.add('hidden');
}

/** Download the office as a file. The daemon sets the filename. */
function exportOffice() {
  const a = document.createElement('a');
  a.href = '/api/export';
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

async function importOffice(ev) {
  const file = (ev.target.files || [])[0];
  ev.target.value = '';                    // so the same file can be re-picked
  if (!file) return;

  let doc;
  try { doc = JSON.parse(await file.text()); }
  catch { return staffError('that file is not JSON'); }

  const incoming = (doc.staff || []).length;
  const going = S.roster.filter(r => !(doc.staff || []).some(e => e.id === r.id));
  const warning = going.length
    ? `\n\n${going.map(r => r.name).join(', ')} ` +
      `${going.length === 1 ? 'is' : 'are'} not in that file and will be let go.`
    : '';
  if (!confirm(`Replace this office with ${incoming} from ${file.name}?` + warning))
    return;

  const res = await fetch('/api/import', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(doc)
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) return staffError(data.error || 'that office file was refused');
  staffError('');
  await loadState();
  buildMap();
  await loadRoster();
}

async function loadRoster() {
  const data = await (await fetch('/api/roster')).json();
  STAFF.roster = data.roster || [];
  STAFF.catalogue = data.catalogue || {};
  STAFF.help = data.tool_help || {};
  renderStaff();
}

function staffError(message) {
  const box = $('staffErr');
  box.textContent = message || '';
  box.classList.toggle('hidden', !message);
}

/** POST to a roster endpoint. The daemon's message is the one worth showing. */
async function staffPost(action, body) {
  const res = await fetch('/api/roster/' + action, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { staffError(data.error || 'that did not work'); return null; }
  staffError('');
  await loadRoster();
  return data;
}

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

function field(label, control, hint) {
  const wrap = el('label', 'sField');
  wrap.appendChild(el('span', 'sLabel', label));
  wrap.appendChild(control);
  if (hint) wrap.appendChild(el('span', 'sHint', hint));
  return wrap;
}

function select(options, value, labeller) {
  const node = el('select');
  for (const opt of options) {
    const o = el('option', null, labeller ? labeller(opt) : opt);
    o.value = opt;
    if (opt === value) o.selected = true;
    node.appendChild(o);
  }
  return node;
}

/** Skills are a long list from several sources, so this one filters. */
function skillPicker(chosen) {
  const all = STAFF.catalogue.skills || [];
  const picked = new Set(chosen || []);
  const box = el('div', 'skPick');

  const search = el('input', 'skSearch');
  search.placeholder = `Filter ${all.length} skills…`;
  const list = el('div', 'skList');

  const draw = () => {
    const q = search.value.trim().toLowerCase();
    list.innerHTML = '';
    // Anything granted stays visible even when filtered out, so a skill can
    // always be un-ticked without first guessing the search term back.
    const rows = all.filter(sk =>
      picked.has(sk.id) || !q
      || sk.id.toLowerCase().includes(q)
      || (sk.description || '').toLowerCase().includes(q));
    if (!rows.length) { list.appendChild(el('div', 'sHint', 'no match')); return; }
    for (const sk of rows.slice(0, 60)) {
      const row = el('label', 'skRow' + (picked.has(sk.id) ? ' on' : ''));
      const cb = el('input');
      cb.type = 'checkbox'; cb.checked = picked.has(sk.id);
      cb.onchange = () => {
        cb.checked ? picked.add(sk.id) : picked.delete(sk.id);
        draw();
      };
      const meta = el('div', 'skMeta');
      meta.appendChild(el('div', 'skName', sk.id));
      if (sk.description) meta.appendChild(el('div', 'skDesc', sk.description));
      row.append(cb, meta);
      list.appendChild(row);
    }
  };
  search.oninput = draw;

  // A plugin's declared name does not always match its directory, so the
  // derived id can be wrong. Typing the exact name has to stay possible.
  const manual = el('div', 'skManual');
  const free = el('input', 'skSearch');
  free.placeholder = 'or type an exact name, e.g. vercel:nextjs';
  const add = el('button', 'ghost', 'Add');
  add.onclick = (e) => {
    e.preventDefault();
    const v = free.value.trim();
    if (v) { picked.add(v); free.value = ''; draw(); }
  };
  free.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); add.click(); } };
  manual.append(free, add);

  box.append(search, list, manual);
  box._chosen = picked;
  draw();
  return box;
}

function checkboxes(names, chosen, help) {
  const box = el('div', 'sChecks');
  const picked = new Set(chosen || []);
  for (const name of names) {
    const label = el('label', 'sCheck');
    const input = el('input');
    input.type = 'checkbox';
    input.value = name;
    input.checked = picked.has(name);
    label.append(input, el('span', null, name));
    if (help && help[name]) label.title = help[name];
    box.appendChild(label);
  }
  return box;
}

const chosenFrom = (box) =>
  [...box.querySelectorAll('input:checked')].map(i => i.value);

function modelLabel(value) {
  if (value) return value;
  const fallback = (STAFF.catalogue || {}).default_model || 'default';
  return `default (${fallback})`;
}

function renderStaff() {
  const list = $('staffList');
  list.innerHTML = '';
  for (const person of STAFF.roster) {
    list.appendChild(
      STAFF.editing === person.id ? editorCard(person) : summaryCard(person));
  }
  renderHire();
}

function summaryCard(person) {
  const card = el('div', 'sCard');
  const head = el('div', 'sHead');

  const who = el('div', 'sWho');
  who.innerHTML =
    `<span class="sEmoji">${person.emoji}</span>` +
    `<b style="color:${person.color}">${escapeHtml(person.name)}</b>` +
    `<span class="muted">${escapeHtml(person.title)}</span>`;

  const tags = el('div', 'sTags');
  tags.appendChild(el('span', 'sTag sTagModel', modelLabel(person.model)));
  tags.appendChild(el('span', 'sTag', `effort ${person.effort}`));
  tags.appendChild(el('span', 'sTag', `${person.max_turns} turns`));
  const skills = [...person.office_tools, ...person.native_tools];
  const agentSkills = person.skills || [];
  tags.appendChild(el('span', 'sTag sTagSkills',
    skills.length ? `${skills.length} tools` : 'no tools'));
  if (agentSkills.length)
    tags.appendChild(el('span', 'sTag sTagAgentSkills',
      `${agentSkills.length} skill${agentSkills.length === 1 ? '' : 's'}`));

  const actions = el('div', 'sActions');
  const edit = el('button', 'ghost', 'Edit');
  edit.onclick = () => { STAFF.editing = person.id; staffError(''); renderStaff(); };
  actions.appendChild(edit);
  if (!person.manager) {
    const fire = el('button', 'sDanger', 'Let go');
    fire.onclick = () => {
      if (confirm(`Let ${person.name} go? Their desk frees up. Past work stays on ` +
                  `the record, and anything they are doing right now fails.`)) {
        staffPost('fire', { id: person.id });
      }
    };
    actions.appendChild(fire);
  }

  head.append(who, tags, actions);
  card.appendChild(head);
  card.appendChild(el('div', 'sSkills', skills.join(' · ') || '—'));
  if (agentSkills.length) {
    const row = el('div', 'sAgentSkills');
    row.appendChild(el('span', 'sAgentSkillsLabel', 'skills'));
    for (const sk of agentSkills) row.appendChild(el('span', 'skChip', sk));
    card.appendChild(row);
  }
  return card;
}

function editorCard(person) {
  const card = el('div', 'sCard sCardOpen');
  const form = el('div', 'sForm');

  const name = el('input'); name.value = person.name; name.maxLength = 40;
  const title = el('input'); title.value = person.title; title.maxLength = 60;
  const emoji = el('input'); emoji.value = person.emoji; emoji.maxLength = 8;
  const model = select(STAFF.catalogue.models, person.model, modelLabel);
  const effort = select(STAFF.catalogue.efforts, person.effort);
  const turns = el('input'); turns.type = 'number';
  turns.min = 1; turns.max = 40; turns.value = person.max_turns;

  const officeTools = checkboxes(
    STAFF.catalogue.office_tools, person.office_tools, STAFF.help);
  const nativeTools = checkboxes(STAFF.catalogue.native_tools, person.native_tools);
  const skillBox = skillPicker(person.skills);
  const persona = el('textarea');
  persona.value = person.persona;
  persona.rows = 10;

  form.append(
    field('Name', name),
    field('Job title', title),
    field('Emoji', emoji),
    field('Model', model, 'Upgrade for hard work; downgrade to spend less.'),
    field('Effort', effort, 'Higher effort means more thinking per task.'),
    field('Max turns', turns, 'Hard cap on tool round trips per task.'),
    field('Office tools', officeTools, 'Hover a name for what it does.'),
    field('Claude Code tools', nativeTools,
          'Bash, Write and Edit are gated by the approval queue.'),
    field('Skills', skillBox,
          'Agent Skills, granted per employee. Each one enabled adds its '
          + 'description to every request this person runs, so grant few.'),
    field('Persona', persona, 'This is the entire system prompt for this employee.'),
  );

  const actions = el('div', 'sActions sActionsWide');
  const save = el('button', 'btn-ok', 'Save');
  save.onclick = async () => {
    const ok = await staffPost('update', {
      id: person.id,
      name: name.value, title: title.value, emoji: emoji.value,
      model: model.value, effort: effort.value, max_turns: turns.value,
      office_tools: chosenFrom(officeTools),
      native_tools: chosenFrom(nativeTools),
      skills: [...skillBox._chosen],
      persona: persona.value,
    });
    if (ok) { STAFF.editing = null; renderStaff(); }
  };
  const cancel = el('button', 'ghost', 'Cancel');
  cancel.onclick = () => { STAFF.editing = null; staffError(''); renderStaff(); };
  actions.append(save, cancel);

  card.append(el('div', 'sHeadOpen', `${person.emoji} ${person.name}`), form, actions);
  return card;
}

function renderHire() {
  const box = $('staffHire');
  box.innerHTML = '';
  const free = (STAFF.catalogue.free_desks || []).length;

  if (STAFF.editing !== '+') {
    const card = el('div', 'sCard sHireRow');
    const button = el('button', 'pill', free ? '+ Hire someone' : 'No free desks');
    button.disabled = !free;
    button.onclick = () => { STAFF.editing = '+'; staffError(''); renderStaff(); };
    card.append(button, el('span', 'muted', `${free} desk${free === 1 ? '' : 's'} free`));
    box.appendChild(card);
    return;
  }

  const card = el('div', 'sCard sCardOpen');
  const form = el('div', 'sForm');
  const name = el('input'); name.placeholder = 'Dana'; name.maxLength = 40;
  const title = el('input'); title.placeholder = 'Security'; title.maxLength = 60;
  const emoji = el('input'); emoji.placeholder = '🔐'; emoji.maxLength = 8;
  const model = select(STAFF.catalogue.models, '', modelLabel);
  const effort = select(STAFF.catalogue.efforts, 'low');
  const turns = el('input'); turns.type = 'number';
  turns.min = 1; turns.max = 40; turns.value = 8;
  const officeTools = checkboxes(STAFF.catalogue.office_tools,
                                 ['note', 'ask_human', 'finish'], STAFF.help);
  const nativeTools = checkboxes(STAFF.catalogue.native_tools, []);
  const skillBox = skillPicker([]);
  const persona = el('textarea');
  persona.rows = 10;
  persona.placeholder =
    'You are Dana, the security engineer. You review changes for injection, '
    + 'secrets and privilege problems, and you report what you found plainly.\n\n'
    + 'Write it the way you would brief a new hire: what they own, how they '
    + 'work, and when to stop and ask.';

  form.append(
    field('Name', name),
    field('Job title', title),
    field('Emoji', emoji, 'Shown on the floor and in the feed.'),
    field('Model', model),
    field('Effort', effort),
    field('Max turns', turns),
    field('Office tools', officeTools),
    field('Claude Code tools', nativeTools),
    field('Skills', skillBox, 'Optional. Each one adds its description to '
          + 'every request this person runs.'),
    field('Persona', persona, 'At least 20 characters. This is their whole brief.'),
  );

  const actions = el('div', 'sActions sActionsWide');
  const hire = el('button', 'btn-ok', 'Hire');
  hire.onclick = async () => {
    const ok = await staffPost('hire', {
      name: name.value, title: title.value, emoji: emoji.value,
      model: model.value, effort: effort.value, max_turns: turns.value,
      office_tools: chosenFrom(officeTools),
      native_tools: chosenFrom(nativeTools),
      skills: [...skillBox._chosen],
      persona: persona.value,
    });
    if (ok) { STAFF.editing = null; renderStaff(); }
  };
  const cancel = el('button', 'ghost', 'Cancel');
  cancel.onclick = () => { STAFF.editing = null; staffError(''); renderStaff(); };
  actions.append(hire, cancel);

  card.append(el('div', 'sHeadOpen', 'New hire'), form, actions);
  box.appendChild(card);
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
  const sp = V.sprites[id];
  const mood = sp ? MOODS[expressionFor(r, sp)] : '';
  const skills = [...(r.office_tools || []), ...(r.native_tools || [])];
  const risky = (r.native_tools || []).filter(t => ['Bash', 'Write', 'Edit'].includes(t));
  $('inspectorHead').innerHTML = `
    <div class="agentCard">
      <div class="agentTop">
        <span class="agentDot" style="background:${r.color}"></span>
        <div>
          <div class="name">${r.emoji} ${escapeHtml(r.name)}</div>
          <div class="role">${escapeHtml(r.title)}</div>
        </div>
        <button class="ghost agentTell" id="tellAgent">Give a job</button>
      </div>
      <div class="badges">
        <span class="badge">${escapeHtml(a.status || 'idle')}</span>
        <span class="badge">${escapeHtml(r.model_id || '')}</span>
        <span class="badge">effort ${escapeHtml(r.effort || '')}</span>
        ${risky.length ? `<span class="badge warn">${risky.join(' · ')}</span>` : ''}
      </div>
      ${mood ? `<div class="agentMood">${escapeHtml(mood)}</div>` : ''}
      ${a.detail ? `<div class="agentDetail">${escapeHtml(a.detail)}</div>` : ''}
      <div class="agentSkills">${escapeHtml(skills.join(' · ') || 'no tools')}</div>
    </div>`;
  const tell = $('tellAgent');
  if (tell) tell.onclick = () => {
    const sel = $('msgTo');
    if (sel) { sel.value = id; updateComposerHint(); }
    $('msg').focus();
  };
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
  let who = ev.agent_id === 'user' ? { name: 'You', emoji: '🧑' }
    : (S.byId[ev.agent_id] || { name: ev.agent_id || 'office', emoji: '' });
  const p = ev.payload || {};

  let text, cls = kind;
  if (kind === 'tool') text = `${p.tool}${p.args ? ' · ' + p.args : ''}`;
  else if (kind === 'created') { text = `new task: ${p.title} → ${p.assignee}`; cls = 'tool'; }
  else if (kind === 'updated') {
    text = `task ${p.status}` + (p.status === 'partial' && p.stop
      ? ` (stopped early: ${String(p.stop).replace(/_/g, ' ')})` : '');
    cls = 'tool';
  }
  else if (kind === 'requested') { text = `needs approval: ${p.action}`; cls = 'error'; }
  else if (kind === 'decided') { text = `approval ${p.status}`; cls = 'tool'; }
  else if (kind === 'break') { text = `heads to the break room — "${p.line}"`; cls = 'social'; }
  else if (kind === 'return') { text = `back at their desk — "${p.line}"`; cls = 'social'; }
  else if (kind === 'summoned') { text = `called into the manager's office (${p.reason})`; cls = 'error'; }
  else if (kind === 'scold') { text = `Miles: "${p.line}"`; cls = 'error'; }
  else if (kind === 'dismissed') { text = `sent back to their desk`; cls = 'social'; }
  else if (kind === 'patrol') { text = `walks the floor`; cls = 'social'; }
  else if (kind === 'changed') {
    // Roster events are about the office, not the person. Rebind rather than
    // mutate: `who` is the live S.byId entry when the agent still exists.
    who = { name: 'office', emoji: '🏢' };
    text = p.action === 'hired' ? `${p.name} joined as ${p.title}`
      : p.action === 'fired' ? `${p.name} left the office`
      : `${p.name}'s setup changed`;
    cls = 'social';
  }
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
