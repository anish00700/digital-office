"""The orchestrator: staff, queues, delegation, approvals, and the budget guard.

One asyncio loop owns everything here. The HTTP layer lives on other threads and
reaches in through `submit_*` / `decide_*`, which marshal onto this loop.
"""

import asyncio
import contextlib
import logging
import time

from . import config, llm, notebook, roster, tools
from .bus import EventBus
from .social import SocialLife
from .store import Store

log = logging.getLogger("office")

WAIT_TIMEOUT_S = 900


class AgentContext:
    """Per-task handle passed to every tool and to the permission gate."""

    def __init__(self, office, agent_id, task_id=None, task_title=""):
        self.office = office
        self.store = office.store
        self.agent_id = agent_id
        self.task_id = task_id
        self.task_title = task_title
        self.result = ""

    # -- output --------------------------------------------------------
    def emit(self, kind, **payload):
        self.office.bus.publish(f"agent.{kind}", agent_id=self.agent_id,
                                task_id=self.task_id, **payload)
        text = payload.get("text")
        if text and kind in ("say", "thinking", "error"):
            self.store.add_transcript(self.agent_id, kind, text, self.task_id)

    def log_stderr(self, line):
        if line and line.strip():
            log.debug("[%s] %s", self.agent_id, line.strip())

    def run_coroutine(self, coro):
        """Run a coroutine from a worker thread (used by the api backend)."""
        return asyncio.run_coroutine_threadsafe(coro, self.office.loop).result()

    # -- human in the loop ---------------------------------------------
    async def request_approval(self, kind, action, detail=""):
        return await self.office.request_approval(self, kind, action, detail)

    async def ask_human(self, question):
        return await self.office.ask_human(self, question)

    async def permission_callback(self, tool_name, input_data, context):
        return await tools.permission_gate(self, tool_name, input_data, context)


class Office:
    def __init__(self):
        self.store = Store()
        self.bus = EventBus(self.store)
        self.backend = llm.get_backend()
        self.loop = None
        self.started_at = time.time()
        self.queues = {}
        self._task_events = {}
        self._approval_waiters = {}
        self._workers = []
        self._staff_tasks = {}      # agent_id -> its worker task, so we can fire one
        self._paused_reason = ""
        self.social = SocialLife(self)

    # -- lifecycle ------------------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        config.WORKSPACE.mkdir(parents=True, exist_ok=True)
        # Seeds from config.DEFAULT_ROSTER on first boot; the table wins after.
        roster.load(self.store)
        self.store.ensure_agents([r.id for r in config.ROSTER])
        self.queues[config.MANAGER_ID] = asyncio.Queue()
        self._workers = [
            asyncio.create_task(self._manager_loop(), name="manager"),
            asyncio.create_task(self._ticker(), name="ticker"),
            asyncio.create_task(self.social.run(), name="social"),
        ]
        for rid in config.STAFF_IDS:
            self._spawn_worker(rid)
        self.bus.publish("office.started", backend=self.backend.name,
                         auth=self.backend.describe_auth())
        log.info("office open - backend=%s auth=%s", self.backend.name,
                 self.backend.describe_auth())

    async def stop(self):
        running = self._workers + list(self._staff_tasks.values())
        for task in running:
            task.cancel()
        for task in running:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- hiring and firing ------------------------------------------------
    # The store write happens on the calling (HTTP) thread - Store has its own
    # lock - and only the worker lifecycle is marshalled onto the office loop.
    def hire_employee(self, fields):
        role = roster.hire(self.store, fields)
        self.store.ensure_agent(role.id)
        self.loop.call_soon_threadsafe(self._spawn_worker, role.id)
        self.bus.publish("roster.changed", action="hired", agent_id=role.id,
                         name=role.name, title=role.title)
        log.info("hired %s (%s)", role.id, role.title)
        return role

    def fire_employee(self, agent_id):
        role = roster.get(agent_id)
        roster.fire(self.store, agent_id)
        self.loop.call_soon_threadsafe(self._retire_worker, agent_id)
        self.bus.publish("roster.changed", action="fired", agent_id=agent_id,
                         name=role.name)
        log.info("let %s go", agent_id)
        return role

    def update_employee(self, agent_id, fields):
        """Model, tools, persona and the rest. No worker restart needed: the
        worker re-reads its role before each task, so this lands on the next
        one rather than mutating a request already in flight."""
        role = roster.update(self.store, agent_id, fields)
        self.bus.publish("roster.changed", action="updated", agent_id=agent_id,
                         name=role.name)
        log.info("updated %s", agent_id)
        return role

    def principal(self):
        """Who this office works for, as the owner set it on first run."""
        return roster.principal(self.store)

    def install_pack(self, pack_id, principal_text=""):
        roster.install_pack(self.store, pack_id, principal_text)
        for rid in config.STAFF_IDS:
            self.store.ensure_agent(rid)
            self.loop.call_soon_threadsafe(self._spawn_worker, rid)
        self.bus.publish("roster.changed", action="setup", agent_id="",
                         name=config.PACKS.get(pack_id, {}).get("name", pack_id))
        log.info("office set up with the %s pack", pack_id)

    def export_office(self):
        return roster.export_office(self.store)

    def import_office(self, doc):
        """Restore a roster from a file. Workers are reconciled afterwards so
        imported staff start working and departed ones stop."""
        installed, retired = roster.import_office(self.store, doc)
        for rid in installed:
            if rid == config.MANAGER_ID:
                continue
            self.store.ensure_agent(rid)
            self.loop.call_soon_threadsafe(self._spawn_worker, rid)
        for rid in retired:
            self.loop.call_soon_threadsafe(self._retire_worker, rid)
        self.bus.publish("roster.changed", action="imported", agent_id="",
                         name=f"{len(installed)} on staff")
        log.info("imported office: %d installed, %d retired",
                 len(installed), len(retired))
        return installed, retired

    @staticmethod
    def tool_help():
        """One-line description per office tool, for the staff panel."""
        return tools.describe_tools()

    def _spawn_worker(self, agent_id):
        """On the office loop. Idempotent."""
        existing = self._staff_tasks.get(agent_id)
        if existing is not None and not existing.done():
            return
        self.queues.setdefault(agent_id, asyncio.Queue())
        self._staff_tasks[agent_id] = asyncio.create_task(
            self._worker_loop(agent_id), name=f"worker:{agent_id}")

    def _retire_worker(self, agent_id):
        """On the office loop. Cancels the worker and releases anything it was
        holding, so a `wait` on their task does not hang for the full timeout."""
        task = self._staff_tasks.pop(agent_id, None)
        if task is not None:
            task.cancel()
        self.queues.pop(agent_id, None)
        self.social.forget(agent_id)
        for row in self.store.tasks(limit=200):
            if row["assignee"] != agent_id or row["status"] not in ("queued", "running"):
                continue
            self.store.update_task(row["id"], status="failed",
                                   error=f"{agent_id} left the office",
                                   finished_at=time.time())
            self.bus.publish("task.updated", agent_id=agent_id, task_id=row["id"],
                             status="failed")
            event = self._task_events.get(row["id"])
            if event is not None:
                event.set()

    # -- inbound from the GUI -------------------------------------------
    def submit_user_message(self, text, to=None):
        """Thread-safe: called from the HTTP layer.

        `to` addresses one employee directly. Miles is still the default and
        still the right answer for most things - he decides who does what - but
        when you already know who you want, routing through him costs a whole
        extra model call to be told what you just said.
        """
        to = (to or "").strip()
        if to and to != config.MANAGER_ID and to in config.STAFF_IDS:
            self.store.add_message("user", to, text)
            self.bus.publish("user.message", text=text, to=to)
            title = " ".join(text.split())[:60] or "Direct request"
            asyncio.run_coroutine_threadsafe(
                self.assign(to, title, text, created_by="user"), self.loop)
            return
        self.store.add_message("user", config.MANAGER_ID, text)
        self.bus.publish("user.message", text=text)
        self.loop.call_soon_threadsafe(self.queues[config.MANAGER_ID].put_nowait, text)

    def decide_approval(self, approval_id, approved, response=""):
        row = self.store.decide_approval(
            approval_id, "approved" if approved else "denied", response)
        if row is None:
            return None
        self.bus.publish("approval.decided", agent_id=row["agent_id"],
                         task_id=row["task_id"], id=approval_id,
                         status=row["status"], response=response)
        waiter = self._approval_waiters.get(approval_id)
        if waiter is not None:
            self.loop.call_soon_threadsafe(waiter.set_result_safe, (approved, response))
        return row

    # -- delegation ------------------------------------------------------
    async def assign(self, assignee, title, brief, created_by):
        # Fail loudly rather than queueing for someone who does not work here:
        # an unclaimed queue has no worker, so the task would sit until the
        # requester's 15-minute wait expired with nothing to show for it.
        if assignee != config.MANAGER_ID and assignee not in config.STAFF_IDS:
            raise KeyError(f"no employee {assignee!r}")
        task_id = self.store.create_task(title, brief, assignee, created_by)
        self._task_events[task_id] = asyncio.Event()
        self.bus.publish("task.created", agent_id=assignee, task_id=task_id,
                         title=title, assignee=assignee, created_by=created_by)
        self.bus.publish("handoff", agent_id=created_by, task_id=task_id,
                         to=assignee)
        # setdefault: a brand-new hire's worker may not have spawned yet.
        await self.queues.setdefault(assignee, asyncio.Queue()).put(task_id)
        return task_id

    async def wait_for(self, task_ids, requester):
        if not task_ids:
            task_ids = [
                t["id"] for t in self.store.tasks(limit=50)
                if t["created_by"] == requester and t["status"] in ("queued", "running")
            ]
        if not task_ids:
            return {}
        self._set_status(requester, "waiting", f"on {len(task_ids)} task(s)")
        events = [self._task_events.get(t) for t in task_ids]
        pending = [e.wait() for e in events if e is not None]
        if pending:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*pending), WAIT_TIMEOUT_S)
        self._set_status(requester, "working", "")
        return {t: (self.store.task(t) or {}) for t in task_ids}

    # -- human in the loop ----------------------------------------------
    async def request_approval(self, ctx, kind, action, detail=""):
        approved, _ = await self._await_human(ctx, kind, action, detail)
        return approved

    async def ask_human(self, ctx, question):
        _, response = await self._await_human(ctx, "question", question, "")
        return response

    async def _await_human(self, ctx, kind, action, detail):
        approval_id = self.store.create_approval(
            ctx.agent_id, kind, action, detail, ctx.task_id)
        future = _SafeFuture(self.loop)
        self._approval_waiters[approval_id] = future
        self.bus.publish("approval.requested", agent_id=ctx.agent_id,
                         task_id=ctx.task_id, id=approval_id, kind=kind,
                         action=action, detail=detail)
        self._set_status(ctx.agent_id, "blocked", action[:80])
        try:
            approved, response = await asyncio.wait_for(
                future.wait(), config.APPROVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.store.decide_approval(approval_id, "expired")
            self.bus.publish("approval.decided", agent_id=ctx.agent_id,
                             id=approval_id, status="expired")
            approved, response = False, ""
        finally:
            self._approval_waiters.pop(approval_id, None)
            self._set_status(ctx.agent_id, "working", ctx.task_title[:60])
        return approved, response

    # -- worker loops ----------------------------------------------------
    async def _worker_loop(self, agent_id):
        queue = self.queues[agent_id]
        while True:
            task_id = await queue.get()
            try:
                # Re-read every time: the staff panel may have changed this
                # employee's model, tools or persona since the last task.
                role = config.role(agent_id)
            except KeyError:
                queue.task_done()
                return                      # fired while this was queued
            try:
                await self._run_task(role, task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("worker %s crashed on %s", agent_id, task_id)
                self.store.update_task(task_id, status="failed", error=str(exc),
                                       finished_at=time.time())
                self.bus.publish("task.updated", agent_id=agent_id, task_id=task_id,
                                 status="failed")
                self._on_failure(agent_id, task_id, str(exc))
            finally:
                event = self._task_events.get(task_id)
                if event is not None:
                    event.set()
                self._set_status(agent_id, "idle", "")
                queue.task_done()

    async def _run_task(self, role, task_id):
        task = self.store.task(task_id)
        if task is None:
            return
        blocked = self._budget_block()
        if blocked:
            self.store.update_task(task_id, status="failed", error=blocked,
                                   finished_at=time.time())
            self.bus.publish("task.updated", agent_id=role.id, task_id=task_id,
                             status="failed")
            return

        self.store.update_task(task_id, status="running", started_at=time.time())
        self._set_status(role.id, "working", task["title"][:60], task_id)
        self.bus.publish("task.updated", agent_id=role.id, task_id=task_id,
                         status="running")

        ctx = AgentContext(self, role.id, task_id, task["title"])
        request = llm.RunRequest(
            agent_id=role.id,
            system=(config.fill(role.persona, self.principal())
                    + notebook.lesson_block(self.store, role.id)),
            prompt=task["brief"],
            tools=tools.specs_for(role),
            native_tools=role.native_tools,
            skills=role.skills,
            model=role.model_id,
            effort=role.effort,
            max_turns=role.max_turns,
            budget_usd=config.TASK_BUDGET_USD,
            cwd=str(config.WORKSPACE),
        )
        turn = await self.backend.run(request, ctx, ctx.emit)
        self._record_usage(role, turn)

        result = ctx.result or turn.text or "(no output)"
        status = "failed" if turn.error else "done"
        # The office records what happened itself. Paying an agent to summarise
        # what the database already knows would be an absurd way to spend a turn.
        with contextlib.suppress(Exception):
            notebook.log_activity(
                self.store,
                f"**{role.name}** {status} — {ctx.task_title}"
                + (f" ({turn.error.splitlines()[0][:70]})" if turn.error else ""))
        self.store.update_task(task_id, status=status, result=result,
                               error=turn.error or None, finished_at=time.time())
        self.bus.publish("task.updated", agent_id=role.id, task_id=task_id,
                         status=status, result=result[:400])
        if status == "failed":
            self._on_failure(role.id, task_id, turn.error)

    async def _manager_loop(self):
        role = config.role(config.MANAGER_ID)
        queue = self.queues[config.MANAGER_ID]
        while True:
            text = await queue.get()
            try:
                await self._run_manager(role, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("manager crashed")
                self._say_to_user(f"I hit an internal error: {exc}")
            finally:
                self._set_status(role.id, "idle", "")
                queue.task_done()

    async def _run_manager(self, role, text):
        blocked = self._budget_block()
        if blocked:
            self._say_to_user(blocked)
            return
        self._set_status(role.id, "thinking", "reading your request")
        ctx = AgentContext(self, role.id, None, "inbox")
        request = llm.RunRequest(
            agent_id=role.id,
            system=(config.fill(role.persona, self.principal())
                    + notebook.lesson_block(self.store, role.id)),
            prompt=text,
            tools=tools.specs_for(role),
            native_tools=role.native_tools,
            skills=role.skills,
            model=role.model_id,
            effort=role.effort,
            max_turns=role.max_turns,
            budget_usd=config.TASK_BUDGET_USD * 2,
            cwd=str(config.WORKSPACE),
        )
        turn = await self.backend.run(request, ctx, ctx.emit)
        self._record_usage(role, turn)
        # message_user is the manager's proper exit; fall back to raw text so a
        # reply is never silently swallowed.
        if not ctx.result and (turn.text or turn.error):
            self._say_to_user(turn.error or turn.text)

    # -- helpers ---------------------------------------------------------
    def _say_to_user(self, text):
        self.store.add_message(config.MANAGER_ID, "user", text)
        self.bus.publish("agent.message_user", agent_id=config.MANAGER_ID, text=text)

    def _set_status(self, agent_id, status, detail="", task_id=None):
        self.store.set_agent(agent_id, status, detail, task_id)
        self.bus.publish("agent.status", agent_id=agent_id, status=status,
                         detail=detail, task_id=task_id)
        self.social.note_status(agent_id, status)

    def _on_failure(self, agent_id, task_id, error):
        """A failed task earns a trip to the manager's office. Fire and forget -
        the telling-off must never delay or block real work."""
        asyncio.create_task(self.social.on_task_failed(agent_id, task_id, error))

    def _record_usage(self, role, turn):
        self.store.add_usage_row(role.id, role.model_id, turn)
        # The topbar renders the rolling window and the day, so publish those.
        # Publishing only the all-time figure left the display frozen at
        # whatever it was when the page loaded.
        now = time.time()
        self.bus.publish(
            "usage", agent_id=role.id, cost=turn.cost_usd,
            tokens=(turn.input_tokens + turn.output_tokens
                    + turn.cache_read + turn.cache_write),
            spend={
                "session": self.store.spend(now - config.SESSION_WINDOW_S),
                "day": self.store.spend(now - 86400),
                "total": self.store.spend(),
            })

    def _budget_block(self):
        if self._paused_reason:
            return self._paused_reason
        spent = self.store.spend_since(time.time() - 86400)["usd"]
        if config.DAILY_BUDGET_USD and spent >= config.DAILY_BUDGET_USD:
            reason = (f"Daily budget reached (${spent:.2f} of "
                      f"${config.DAILY_BUDGET_USD:.2f} in the last 24h). "
                      "The office is paused. Raise OFFICE_DAILY_BUDGET_USD or wait.")
            self.bus.publish("office.budget", reason=reason, spend=spent)
            return reason
        return ""

    # -- background ticker -----------------------------------------------
    async def _ticker(self):
        """Fires due reminders. Cheap: no model call unless something is due."""
        while True:
            await asyncio.sleep(30)
            try:
                for row in self.store.due_reminders():
                    self.store.mark_reminder_fired(row["id"])
                    self._say_to_user(f"⏰ Reminder: {row['text']}")
            except Exception:
                log.exception("ticker failed")


class _SafeFuture:
    """A future settable from any thread via call_soon_threadsafe."""

    def __init__(self, loop):
        self.loop = loop
        self._future = loop.create_future()

    def set_result_safe(self, value):
        if not self._future.done():
            self._future.set_result(value)

    async def wait(self):
        return await self._future
