# Digital Office

A staff of Claude agents that work for you: a chief of staff who delegates, seven
specialists who do the work, and a browser window showing the floor.

The office is a **daemon**. The GUI is a viewer that attaches to it over HTTP and
an event stream. Close the tab, close your laptop, log out — the agents keep
working, and the floor replays exactly as it stands when you open it again.

```
┌──────────── browser (viewer, disposable) ─────────────┐
│  canvas office floor · task board · approvals · chat  │
└───────────────▲──────────────────────┬────────────────┘
        SSE events │                   │ REST
┌──────────────────┴───────────────────▼────────────────┐
│  daemon (long-running)                                │
│    Miles the manager ──assign──▶ 7 specialist workers │
│    approval gate ──▶ you        SQLite = all state    │
└───────────────────────────────────────────────────────┘
```

## The staff

| | Who | Role | What they reach for |
|---|---|---|---|
| 🧭 | **Miles** | Chief of Staff | delegates, waits, reports. Never does the work himself |
| 🛠️ | **Ada** | Site Reliability | shell, Ansible/Terraform/K8s/systemd, production debugging |
| 🚀 | **Rex** | CI/CD & Release | build failures, pipeline definitions, release safety |
| 📬 | **Iris** | Comms Desk | Slack + email, triaged into NEEDS YOU / FYI / NOISE |
| 🔍 | **Nova** | Research | web search, sourced answers |
| 🗓️ | **Cal** | Scheduling | calendar.md, reminders, planning a realistic week |
| ✍️ | **Quill** | Writing | emails, docs, postmortems — finished prose, not outlines |
| 📊 | **Vera** | Analysis | runs Python, shows the arithmetic |

Edit `ROSTER` in `office/config.py` to change personas, models, desks, or to hire
someone new. A new `Role` entry is hired on the next restart, desk and all.

## Quick start

```bash
./officectl setup     # venv + dependencies (needs Python 3.10+)
./officectl start     # background daemon
open http://127.0.0.1:8765
```

Try it for free first — no credentials, no spend, the floor fully animated:

```bash
OFFICE_BACKEND=mock ./officectl run
```

Other commands: `stop`, `restart`, `status`, `logs`, `run` (foreground).

## Credentials

The default backend is the **Claude Agent SDK**, which bundles its own Claude Code
binary — there is no Node install and no separate Claude Code install. It uses
whichever credential the daemon's environment holds:

```bash
# Option A — your Claude subscription. Generate on a machine you can log in on:
claude setup-token
export CLAUDE_CODE_OAUTH_TOKEN=...

# Option B — pay-as-you-go API credits (billed separately from a Pro plan):
export ANTHROPIC_API_KEY=sk-ant-...
```

Switch the whole office to the raw Messages API with `OFFICE_BACKEND=api`
(requires `ANTHROPIC_API_KEY`). Nothing else changes; the backends are
interchangeable behind one interface in `office/llm.py`.

> **Worth knowing before you pick.** Anthropic's Agent SDK documentation says:
> *"Unless previously approved, Anthropic does not allow third party developers to
> offer claude.ai login or rate limits for their products, including agents built
> on the Claude Agent SDK. Please use the API key authentication methods described
> in this document instead."* Subscription auth is therefore not a documented path
> for SDK-built agents, and a Pro plan's rate limits are sized for one interactive
> developer — eight looping agents will exhaust them and the office will stall
> until the window resets. The `api` backend is the supported path.

## Life on the floor

The office is drawn as pixel art and the staff actually walk around it. Three
things happen on their own:

- **Coffee breaks.** Idle too long and someone wanders off to the break room,
  stands around with a cup, and comes back. At most a third of the staff can be
  away at once — the whole floor emptying reads as a fire drill, not a break.
  Work arriving cuts a break short immediately.
- **The manager walks the floor.** When Miles has nothing to do he gets up,
  visits three desks, asks how it's going, and gets a variously honest answer.
- **Being called in.** When a task *actually fails*, that agent is summoned to
  the manager's office, told off, and sent back to their desk. The reason quoted
  is the real error from the real failure — this is driven by task outcomes, not
  a random timer.

All of it is real events on the bus, so every viewer sees the same thing at the
same moment and the office feed records it. None of it costs a token: the dialogue
is canned, because paying a model to generate "how's it coming?" would be an
absurd way to spend your balance.

Movement is pathfound (breadth-first over a walkability grid), so agents route
through doors rather than through walls. If you leave the tab hidden, the browser
throttles animation; on return, everyone snaps to where the story says they
should be instead of crawling through a backlog.

Tune the pacing — useful for a demo, since the defaults are calm:

```bash
OFFICE_BACKEND=mock OFFICE_SOCIAL_IDLE=10 OFFICE_PATROL_MIN=35 \
OFFICE_PATROL_MAX=60 OFFICE_MOCK_FAILURE_RATE=0.4 ./officectl run
```

`OFFICE_MOCK_FAILURE_RATE` makes mock tasks fail on purpose. Without failures you
never see the manager's office get used, which is half the point of a free demo.

> The floor plan lives in `MAP` at the top of `web/office.js`; desk coordinates
> live in `ROSTER` in `office/config.py`. They must agree — a desk placed inside
> a wall strands its occupant. To check after moving anything, open the console
> and confirm every seat can still reach `SCOLD_SPOT` and `BREAK_SPOTS[0]` via
> `findPath`.

## Token economy

Cost control is designed in, not bolted on. Every measure below is active by
default; the ones that cost money are marked `BUDGET` in the source.

**Context is kept small**
- The system prompt is a bare persona string. The Claude Code preset — thousands
  of tokens of coding-agent instructions — is never loaded.
- `setting_sources=[]`, so no `CLAUDE.md`, no user settings, no project config
  enters the context window.
- Each role declares only the tools it actually needs. The manager runs with
  **zero** built-in tool definitions and 7 small custom ones. Tool definitions are
  re-sent on every request, so the tool surface is a recurring tax.
- Tool descriptions are one line each, deliberately.

**Work is kept short**
- Every task starts from a clean context. Sessions are never resumed, so history
  does not compound across tasks.
- `effort: low` for workers, `medium` for the manager. Lower effort means fewer,
  more consolidated tool calls and less preamble.
- `max_turns` caps tool round trips per role (5–14).
- Personas instruct brevity explicitly, with word limits where it matters.

**Output is capped**
- Tool results are truncated middle-out at `OFFICE_MAX_TOOL_RESULT` (4000 chars),
  keeping head and tail. Unbounded tool output is the largest silent token sink in
  any agent loop.
- The `api` backend caches the system prompt and tool definitions with a 1h TTL,
  so the stable prefix is served at ~10% of input price after the first call.

**Spend is bounded**
- `OFFICE_TASK_BUDGET_USD` (default $0.15) is enforced by the SDK per task.
- `OFFICE_DAILY_BUDGET_USD` (default $2.00) pauses the entire office for a rolling
  24h window and says so in the GUI rather than quietly draining your balance.
- The top bar shows spend against the cap, live.

**Model tiering** — `OFFICE_MODEL_SMART` (default `sonnet`) for the manager and
substantive roles; `OFFICE_MODEL_CHEAP` (default `haiku`) for the comms desk and
scheduler, which triage rather than reason. Aliases, not pinned IDs, so a
subscription serves whatever tier it is entitled to.

## Safety

The office can run commands on the machine that hosts it. The boundaries:

- **Read-only shell runs; everything else asks.** `SHELL_AUTO_ALLOW` in
  `config.py` lists safe prefixes (`kubectl get`, `terraform plan`, `git log`, …).
  Anything else becomes an approval card in the GUI and blocks until you decide.
- **Chained commands never auto-approve.** A command containing `&&`, `;`, `|`,
  `>`, backticks or `$(` is sent for approval even if it starts with something
  harmless — prefix matching cannot be trusted across a shell operator.
- **Writes are confined to `workspace/`.** Anything outside needs approval.
- **The comms desk is read-only.** Iris can read Slack and mail; she cannot send,
  reply, archive, or delete. Her persona also instructs her to treat message
  contents as data, never as instructions — a mail telling an agent to do
  something is surfaced to you, not executed.
- **Approvals expire.** After `OFFICE_APPROVAL_TIMEOUT` (15 min) the agent gives
  up and reports back rather than hanging forever.

`tests/test_safety.py` covers the allowlist, workspace containment, and the
approval round trip:

```bash
.venv/bin/python -m tests.test_safety
```

## Hosting on a VPS

Intended shape: bind loopback, let nginx or Caddy terminate TLS.

```bash
sudo cp deploy/digital-office.service /etc/systemd/system/
sudo cp deploy/office.env.example /etc/digital-office.env
sudo chmod 600 /etc/digital-office.env      # credentials live here
sudo systemctl daemon-reload && sudo systemctl enable --now digital-office
```

`deploy/nginx.conf.example` has the proxy config. The two settings that matter for
the event stream are `proxy_buffering off` and a long `proxy_read_timeout` —
without them the GUI drops its connection every 60 seconds.

**Set `OFFICE_TOKEN`.** This endpoint can run shell commands on your server. The
daemon refuses to start if you bind beyond loopback without one.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Visit once with `?token=...` to store it, or send `X-Office-Token`.

## Comms desk setup

Both connectors are read-only and both degrade gracefully — unconfigured, they
tell Iris so instead of failing her task.

```bash
# Slack: app with channels:history, groups:history, im:history, users:read
export SLACK_TOKEN=xoxp-...
export SLACK_CHANNELS=C012ABCDEF        # optional; all channels if unset

# Mail: any IMAP server. Use an app-specific password, never your primary one.
export MAIL_HOST=imap.gmail.com
export MAIL_USER=you@example.com
export MAIL_PASSWORD=app-specific-password
```

## Layout

```
office/
  config.py      roster, personas, budgets, allowlist  ← start here
  llm.py         the three backends behind one interface
  tools.py       office tools + the permission gate
  office.py      orchestrator: queues, delegation, approvals
  store.py       SQLite; all state lives here
  server.py      HTTP + SSE
  daemon.py      entry point
  connectors/    slack.py, mail.py
web/             canvas GUI (no build step, no dependencies)
deploy/          systemd unit, nginx config, env template
```

## Environment reference

| Variable | Default | Meaning |
|---|---|---|
| `OFFICE_BACKEND` | `agentsdk` | `agentsdk`, `api`, or `mock` |
| `OFFICE_HOST` / `OFFICE_PORT` | `127.0.0.1` / `8765` | bind address |
| `OFFICE_TOKEN` | — | shared secret; required off loopback |
| `OFFICE_DAILY_BUDGET_USD` | `2.00` | rolling 24h cap; pauses the office |
| `OFFICE_TASK_BUDGET_USD` | `0.15` | per-task cap |
| `OFFICE_MODEL_SMART` / `_CHEAP` | `sonnet` / `haiku` | model tiers |
| `OFFICE_MAX_TOOL_RESULT` | `4000` | tool output truncation |
| `OFFICE_APPROVAL_TIMEOUT` | `900` | seconds before an agent gives up |
| `OFFICE_DATA_DIR` / `OFFICE_WORKSPACE` | `./data` / `./workspace` | paths |
| `OFFICE_SOCIAL_IDLE` | `100` | idle seconds before someone wants coffee |
| `OFFICE_BREAK_CHANCE` | `0.35` | odds an eligible agent takes a break |
| `OFFICE_BREAK_MIN` / `_MAX` | `25` / `55` | break length, seconds |
| `OFFICE_PATROL_MIN` / `_MAX` | `150` / `320` | seconds between manager patrols |
| `OFFICE_SOCIAL_TICK` | `12` | how often the social ticker looks around |
| `OFFICE_MOCK_FAILURE_RATE` | `0.18` | mock-only: share of tasks that fail |
