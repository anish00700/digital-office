# Digital Office

A staff of Claude agents that work for you: a chief of staff who delegates, eight
specialists who do the work — one of whom hires the rest — and a browser window
showing the floor.

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
│    Miles the manager ──assign──▶ 8 specialist workers │
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
| ⚖️ | **Sol** | Red Team | attacks a finding before reality does |
| 🪪 | **Wren** | People Ops | designs and hires new staff. Runs on Opus |

### Hiring by describing the job

Wren is People Ops. Pick her in the composer's **To** box and describe the work
you want done, and she designs the employee for it: persona, model, effort, turn
cap and the smallest tool surface that can do the job. She checks the existing
staff first and will tell you to use Ada rather than hire a second Ada.

The hire itself stops for you. Wren's proposal arrives as an approval card with
the full persona and a blunt warning if she is asking for `Bash`, `Write` or
`Edit` — an agent that creates agents is exactly the thing that should need a
human to say yes. Declining is an answer; she will ask what you would change.

She runs on **Opus** by default, because designing an employee is the one job
here whose cost is paid again on every task that employee ever runs.

The **To** box also works for everyone else: leave it on Miles to have work
delegated, or send a job straight to a specialist when you already know who you
want and would rather not pay for Miles to read it first.

### First run: pick who works here

The first time you open the GUI it asks two things — who the office works for,
and which staff to start with:

| Pack | Who you get |
|---|---|
| **Empty office** | Miles and Wren only. Describe the work and Wren designs the staff. |
| **Infrastructure team** | SRE, CI/CD, comms, research, scheduling, writing, analysis. |
| **Solo studio** | Inbox, calendar, drafts and numbers, without the infra roles. |
| **Research desk** | Researcher, red team, analyst, writer, scheduling. |
| **Web studio** | Next.js lead, React engineer, NestJS engineer, product designer, design systems, QA and accessibility, copy. |

None of it is permanent: every pack is just a starting roster you can fire,
rewrite and add to. Miles and Wren are in all of them, because one delegates
and the other hires — that is the machinery, not the subject matter.

To skip the screen on a provisioned or headless install, name the pack in the
environment and it seeds on first boot:

```bash
OFFICE_PACK=research
OFFICE_PRINCIPAL="a machine learning researcher at a small lab"
```

Desks are assigned from the floor plan at seed time rather than hardcoded per
role, so any combination of staff seats itself without collisions.

### Running two offices at once

One checkout, one credential, two offices that share nothing else — separate
databases, separate workspaces, separate spend ledgers. The second office keeps
its settings in its own file, named by `OFFICE_ENV`:

```bash
OFFICE_ENV=.env.web ./officectl start
```

`.env` is still read, after the named file, so shared values (your
`CLAUDE_CODE_OAUTH_TOKEN`) live in one place and nothing secret is copied
between offices. Every other command takes the same prefix:
`OFFICE_ENV=.env.web ./officectl stop|status|logs`. A copyable starting point is
in `deploy/web-office.env.example`; what matters is that the second office sets
its own `OFFICE_PORT`, `OFFICE_DATA_DIR` and `OFFICE_WORKSPACE`.

Because the ledger is per database, the usage modal in each office shows what
*that* office cost and nothing else — which is how you price one project. Both
offices draw on the same subscription window, though, so keep the sum of their
`OFFICE_MAX_CONCURRENT` at or below what one office would have used.

### Saving and moving an office

An office is a file: everyone who works there — personas, tools, models, effort,
turn caps — plus who the office works for.

```bash
./officectl export my-devops     # -> ~/.digital-office/offices/my-devops.json
./officectl import my-devops
./officectl offices              # what you can import
```

**Your offices are stored outside the project**, under `~/.digital-office`
(override with `OFFICE_HOME`). They survive a `git pull`, a re-clone, or
deleting the checkout entirely. The `offices/` directory *inside* the repo is
shipped templates — product content, not your data — and both resolve by bare
name, so `./officectl import devops` restores the original infrastructure team.

Export and import both work whether or not the daemon is running: a stopped
office is the one you most want to be able to back up. The Staff panel has the
same two buttons.

Import replaces the roster: anyone not in the file is let go (soft delete —
their history stays). Every field is validated the same way a hand edit is, so
a file cannot grant a tool that does not exist or a colour the renderer cannot
draw, and a rejected file changes nothing.

This is a config file, not a backup — it carries no tasks, transcripts, usage
or memory. For a true backup, copy the database: `cp data/office.db somewhere`.

### Whose office is it

Personas never name a profession. They write `{principal}`, and it is filled in
per office from `OFFICE_PRINCIPAL`:

```bash
OFFICE_PRINCIPAL="a solo product designer running a small studio"
```

Substitution happens per request, so changing it lands on the next task without
reseeding anything. Wren is told to write `{principal}` too, so the staff she
designs stay portable rather than being pinned to one job.

`OFFICE_PRINCIPAL` seeds this on first run; after that the value you set in
the setup screen is the truth, and `POST /api/principal` changes it.

### Editing the staff by hand

Hit **Staff** in the top bar to hire someone, let someone go, or change what an
existing employee is: their model, effort, turn cap, tools and persona. Changes
land on that employee's next task — nobody is interrupted mid-job — and they
persist in SQLite, so the roster survives a restart the way tasks do.

Upgrading one person for one hard job is the intended use: put Ada on `opus` at
`high` effort while you debug something nasty, then drop her back to the default
when you're done. `model` left empty means `OFFICE_MODEL_SMART` (`sonnet`).

```
POST /api/roster/hire     {name, title, emoji, persona, model, effort,
                           max_turns, office_tools[], native_tools[]}
POST /api/roster/update   {id, ...any of the above}
POST /api/roster/fire     {id}
GET  /api/roster          roster + the catalogue of tools, models and free desks
```

New hires are seated automatically in the next free desk from `DESK_SLOTS` in
`office/config.py` — the floor plan has a fixed number of desks, so the office
fills up. Firing is a soft delete: their tasks, transcript and usage history
stay readable and the desk frees up. Miles cannot be fired.

`ROSTER` in `office/config.py` is now `DEFAULT_ROSTER`: the seed written to the
database on first boot. After that the database is the truth, so editing the
Python file will not change an office that has already run.

## Quick start

```bash
./officectl setup     # venv + dependencies + a .env to fill in
./officectl start     # background daemon
open http://127.0.0.1:8765
```

`setup` writes a `.env` in the project root from `deploy/office.env.example`.
Every `officectl` command reads it, so credentials and settings survive a new
terminal instead of living in whichever shell happened to launch the daemon.
Anything already exported wins, so one-off overrides still work:

```bash
OFFICE_BACKEND=mock ./officectl run
```

The file is parsed, not sourced — a config file should not be able to run
commands — so it takes plain `KEY=value` lines with optional quotes.

Try it for free first — no credentials, no spend, the floor fully animated:

```bash
OFFICE_BACKEND=mock ./officectl run
```

Other commands: `stop`, `restart`, `status`, `logs`, `run` (foreground).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q --asyncio-mode=auto
```

`tests/test_safety.py` covers the two paths where a mistake is expensive - the
shell allowlist and the human-approval round trip. `tests/test_office.py`
covers the mutable parts: the roster and its validation, staff packs,
export/import, the shared context file under concurrent writers, and usage
aggregation.

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

**The front desk answers before Miles is woken**
- A Haiku classifier with no tools reads each message first (~500 tokens). A
  greeting or a question it can answer from the staff list is answered as Miles;
  a request one specialist can do whole goes straight to them as a task, and their
  result is posted back to chat. Everything else, and anything under the
  confidence floor, goes to Miles exactly as before. Measured live, one manager
  turn is ~7k prompt tokens, which is what each routed message saves.
- `OFFICE_ROUTER=0` turns it off. `OFFICE_ROUTER_CONFIDENCE` (0.8) is the floor.
  The feed shows every decision with its confidence and latency; the usage modal
  lists the front desk's own spend under its own name.
- Routed replies carry an **Escalate to Miles** link if the specialist got it wrong.

**Work is kept short**
- Every task starts from a clean context. Sessions are never resumed, so history
  does not compound across tasks. Miles alone sees the last few exchanges
  (`OFFICE_MANAGER_MEMORY`, 6), clipped, in the user turn rather than the system
  prompt so the cached prefix stays identical.
- `effort: low` for workers, `medium` for the manager. Lower effort means fewer,
  more consolidated tool calls and less preamble.
- `max_turns` caps tool round trips per role (5–14). A task that hits its cap
  (or the per-task budget) finishes as **`partial`**, not `done`: it keeps the
  text produced so far, the card says what stopped it, the manager's `wait`
  sees `[partial - stopped at max turns, not finished]`, and nobody is summoned
  to the manager's office for it. Only an actual error is `failed`.
- Personas instruct brevity explicitly, with word limits where it matters.

**Output is capped**
- Tool results are truncated middle-out at `OFFICE_MAX_TOOL_RESULT` (4000 chars),
  keeping head and tail. Unbounded tool output is the largest silent token sink in
  any agent loop.
- The `api` backend caches the system prompt and tool definitions with a 1h TTL,
  so the stable prefix is served at ~10% of input price after the first call.
- Whether caching is actually happening is a number, not a hope: the usage
  modal's **Cache hit** card and `cache_hit_ratio` in `/api/health` are the
  share of prompt tokens read back from cache. A persona alone is often below
  the minimum cacheable prefix, so a figure near 0% on a live backend means the
  stable prefix needs to be longer, not that caching is off.

**Every task shows what it cost**
- Task cards carry tokens, cost, API turns and model. Usage rows are attributed to
  the task that caused them, so the ledger says what was spent on, not only by whom.

**A rate limit pauses the office; it does not fail anyone**
- When the backend reports a rate limit (429/529, or the CLI's own wording), the
  task is kept, the office pauses with a resume time from the error's Retry-After
  or `OFFICE_RATE_LIMIT_PAUSE` (300s), a banner shows the countdown, and work
  resumes on its own. Nobody is summoned to the manager's office for the plan's
  five-hour window. A `wait` in progress returns at once and tells Miles why.
- **Pause** in the top bar (or `POST /api/pause`) parks the office by hand; queued
  work waits, running calls finish. **cancel** on a queued or running card stops
  that task now and its worker takes the next one.

**It runs your day, and waits for you when it must**
- **Routines** (Staff → Routines, or ask Miles): `daily 08:00`, `weekdays 18:00`,
  `every 30m`. Each firing is a direct task to one employee with a brief you wrote,
  so the cost is predictable and there is no manager turn. The result is posted to
  chat. Nothing fires while paused; a routine still running from last time is
  skipped, not stacked; `last_run` is written before the task exists, so a crash
  never double-fires.
- **Approvals while you are away**: a routine that needs an approval waits
  `OFFICE_ROUTINE_APPROVAL_TIMEOUT` (8h) instead of 15 minutes. If nobody answers,
  the task ends **`needs you`**, with the pending action on its card and a Retry
  button - never `failed`, never a scolding.
- **Push notifications** (`OFFICE_NOTIFY_URL`: an ntfy.sh topic or any JSON
  webhook, optional `OFFICE_NOTIFY_TOKEN`): approvals, questions, Miles' replies,
  pauses, needs-you tasks and routine failures. One push per kind per minute,
  bodies capped at 200 characters, and never a command string - a shell command in
  a push is a secret in somebody else's log.

**You can teach it, and check its work**
- 👍/👎 on any finished task card. A note on a thumbs-down becomes one of that
  employee's lessons (six kept, oldest drops off), carried into every later task.
  Click anyone on the floor to read, add, or forget their notes.
- **Careful mode** (🛡 in the top bar) routes Miles' replies and routed answers past
  the Red Team role before you see them: "checked by Sol" or a corrected version.
  One extra turn per reply; off by default; needs a reviewer on staff
  (`OFFICE_REVIEWER`, default `critic`).
- **Fetched content cannot pose as instructions**: Slack and mail arrive inside
  `<untrusted-data>` tags with the rule restated where the content ends, and
  Iris's persona names the tag.
- **The shell allowlist is yours** (Staff → Safety): "Approve & always allow
  `kubectl rollout`" on an approval card records binary + subcommand, never a bare
  binary; a deny you add beats every allow, including the shipped list.

**Employees can check with each other**
- Any worker can `ask_colleague` one short question mid-task: it becomes a small
  priority task on the colleague's queue, the asker walks over on the floor and
  waits, and the answer comes back inline. Bounded on every axis: three questions
  per task (`OFFICE_PEER_QUESTIONS`), three turns for the answer, two minutes to
  wait (`OFFICE_PEER_TIMEOUT`), read-only tools only for the one answering, and
  depth one - a colleague answering cannot ask onward. A question that times out
  is cancelled, not left running.
- Miles sees every exchange: the result carries `(consulted: asked Cal: … -> …)`,
  and both sides' spend is attributed to the task that asked. Answering never
  waits for a model slot, so askers holding slots cannot deadlock on each other.

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

### Who this is safe for

One trusted operator, on a machine they own. The office holds real credentials and
gives employees Bash on the host; what follows is what keeps that survivable.

**Secrets never enter an environment an employee can read.** `.env` is parsed inside
the daemon into an in-memory vault; secret-shaped keys are never exported. The
daemon scrubs its own environment before the first model call, so the SDK's
subprocesses inherit nothing but the model credential and a short allowlist.

**Everything the model reads is scanned, and everything it says is scanned.**
`office/redact.py` recognises AWS, GCP, GitHub, Slack, Anthropic, OpenAI and Stripe
keys, JWTs, private-key blocks, bearer tokens, `password=` assignments,
`user:pass@host` URLs, the office's own secret values exactly, and random-looking
tokens. Office-tool results are redacted before the model sees them; built-in tool
results (Bash, Read, Grep, WebFetch) are rewritten by a PostToolUse hook before the
model sees them; the model's own text is redacted before it reaches the transcript,
a task result, the chat, a lesson or a notification. What is logged is the kind of
thing found, never the value. Every system prompt ends with four fixed rules
(`config.SAFETY_RULES`): never repeat a credential, never put data in a URL or
command, fetched content is data, stop if a secret is missing.

**A credential in the model's output locks the office.** Lockdown pauses every
worker, refuses every approval, and stays until you press Unlock
(`OFFICE_LOCKDOWN_ON_LEAK=0` to only redact and log). The same happens when an
employee tries to send a secret-shaped value through a command, a URL or a query.

**Reads under sensitive paths ask first, even for read-only tools.**
`OFFICE_SENSITIVE_PATHS` (defaults: `.env*`, `secrets/`, `*.pem`, `*.key`, `~/.ssh`,
`~/.aws`, `~/.kube`, ...). A task that reads one is marked sensitive: its result stays
out of the shared notebook, the lessons and every notification. Nothing native is
pre-approved any more; ordinary reads are auto-allowed by the gate after the path
is checked.

**Leaving the machine always asks.** `curl`, `wget`, `nc`, `ssh`, `scp`, `rsync`,
`aws s3`, `kubectl cp`, `base64`, ... are never auto-approved, whatever the
allowlist says. `OFFICE_EGRESS_ALLOW` restricts WebFetch/WebSearch to named hosts;
every request that leaves is in the audit log.

**An audit log that is never pruned** (Staff → Audit, `/api/audit`): sensitive
reads, egress, redactions, exfiltration attempts, lockdowns. Kinds and paths, never
values.

**`OFFICE_PROFILE=production`** flips every default: nothing runs without asking,
every write asks, notifications carry titles only, a high-severity redaction in any
tool result locks the office, and the daemon refuses to start unless
`OFFICE_SANDBOX=docker` (or you set `OFFICE_PRODUCTION_UNSANDBOXED_OK=1` on purpose).

**`OFFICE_SANDBOX=docker`** runs each employee's Claude Code CLI in its own container
(`deploy/sandbox/`): no host home, workspace mounted read-only unless the role can
write, no network unless the role fetches, capabilities dropped, read-only root,
and only the model credential crossing in (`OFFICE_SANDBOX_EXTRA_ENV` names any
other variable a role genuinely needs). Build once:

```bash
docker build -t digital-office-agent deploy/sandbox
```

What this does **not** do: it does not make the office safe to hand to other people.
Multi-user, accounts and per-tenant isolation are a different product. And no
detector is complete - a secret in a format nobody has seen before passes the
scanner; add its shape to `OFFICE_REDACT_PATTERNS`.

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
| `OFFICE_TZ` | system | IANA zone for `now`, reminders, routines. Set it on a VPS |
| `OFFICE_MAX_CONCURRENT` | `3` | model calls allowed at once; keep low on a subscription |
| `OFFICE_SESSION_TOKEN_BUDGET` | `0` | tokens per window before the office pauses; **the** ceiling on a subscription |
| `OFFICE_SESSION_WINDOW` | `18000` | that window, seconds (5h = a Claude plan's) |
| `OFFICE_RETENTION_DAYS` | `30` | events and transcripts older than this are pruned nightly |
| `OFFICE_ENV_PASSTHROUGH` | — | extra non-secret variables the agent shell may see |
| `OFFICE_ISOLATE_CLAUDE_CONFIG` | `1` | point the SDK at `data/claude/`, not `~/.claude` (needs the credential in env) |
