"""The orchestrator: staff, queues, delegation, approvals, and the budget guard.

One asyncio loop owns everything here. The HTTP layer lives on other threads and
reaches in through `submit_*` / `decide_*`, which marshal onto this loop.
"""

import asyncio
import contextlib
import hashlib
import datetime as dt
import logging
import time

from . import config, llm, notebook, roster, router, tools
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
        self._paused_until = 0.0
        self._resume = None          # asyncio.Event, created on the loop
        self._paused_event = None    # set while paused; wait_for watches it
        self._current = {}           # agent_id -> the asyncio.Task running its job
        self._cancelled = set()      # task ids cancelled while queued or running
        self._last_message = ("", 0.0)
        # One gate for every model call; see config.MAX_CONCURRENT.
        self._model_slots = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._last_prune = 0.0
        self.social = SocialLife(self)

    # -- lifecycle ------------------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self._resume = asyncio.Event()
        self._resume.set()
        self._paused_event = asyncio.Event()
        # Messages are classified one at a time, in the order they arrived.
        # Concurrent classification let a quick second message reach Miles'
        # queue ahead of a slow first one - "actually, cancel that" before
        # the thing it cancelled.
        self._desk_lock = asyncio.Lock()
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
        self._recover_tasks()
        self.bus.publish("office.started", backend=self.backend.name,
                         auth=self.backend.describe_auth())
        log.info("office open - backend=%s auth=%s concurrency=%d tz=%s",
                 self.backend.name, self.backend.describe_auth(),
                 config.MAX_CONCURRENT, config.TZ or "system")

    def _recover_tasks(self):
        """A daemon that died mid-task leaves rows stuck in `running`, and
        `queued` rows nobody holds. Requeue both in creation order. The
        manager's in-flight conversation is not recoverable - it lived in a
        model session that is gone - so tell the user what was re-run."""
        rows = self.store.tasks_by_status(("running", "queued"))
        requeued, dropped = [], 0
        for row in rows:
            if row["assignee"] not in self.queues:
                self.store.update_task(row["id"], status="failed",
                                       error=f"{row['assignee']} is no longer on staff",
                                       finished_at=time.time())
                dropped += 1
                continue
            if row["status"] == "running":
                self.store.update_task(row["id"], status="queued", started_at=None)
                self.bus.publish("task.updated", agent_id=row["assignee"],
                                 task_id=row["id"], status="queued")
            self._task_events[row["id"]] = asyncio.Event()
            self.queues[row["assignee"]].put_nowait(row["id"])
            requeued.append(row["title"])
        if requeued or dropped:
            self.bus.publish("office.recovered", requeued=len(requeued), dropped=dropped)
            names = "; ".join(t[:40] for t in requeued[:5])
            more = f" (+{len(requeued) - 5} more)" if len(requeued) > 5 else ""
            self._say_to_user(
                f"The office restarted. I re-queued {len(requeued)} task(s) that were "
                f"in progress: {names}{more}." if requeued else
                f"The office restarted; {dropped} task(s) belonged to staff who have left.")
            log.info("recovered %d task(s), dropped %d", len(requeued), dropped)

    def health(self):
        """Deep health for a watchdog or a human: is the loop alive, are the
        workers, how deep are the queues, when did anything last happen."""
        now = time.time()
        return {
            "ok": True,
            "backend": self.backend.name,
            "paused": self._paused_reason or None,
            "paused_until": self._paused_until or None,
            "front_desk": bool(config.ROUTER and hasattr(self.backend, "structured")),
            "uptime_s": round(now - self.started_at),
            "workers_alive": {aid: (not t.done()) for aid, t in self._staff_tasks.items()},
            "queue_depth": {aid: q.qsize() for aid, q in self.queues.items()},
            "model_slots_free": self._model_slots._value,
            "last_event_age_s": round(now - (self.store.last_event_ts() or self.started_at)),
            "tz": str(config.TZ or "system"),
            # Share of prompt tokens served from cache in the rolling window.
            # Near zero means the stable prefix is too short to cache at all.
            "cache_hit_ratio": self.cache_hit_ratio(now - config.SESSION_WINDOW_S),
        }

    def cache_hit_ratio(self, since=0.0):
        t = self.store.usage_totals(since)
        prompt = (t.get("input") or 0) + (t.get("cache_read") or 0) + (t.get("cache_write") or 0)
        return round((t.get("cache_read") or 0) / prompt, 3) if prompt else 0.0

    async def stop(self):
        running = self._workers + list(self._staff_tasks.values())
        for task in running:
            task.cancel()
        for task in running:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- pause, resume, cancel ----------------------------------------------
    # All three are safe from any thread: they marshal onto the office loop.
    def pause(self, reason, resume_at=0.0):
        """Stop starting model calls. Running ones finish; queued work waits.
        Nobody is marked failed and nobody is scolded - a pause is the
        office's condition, not an employee's mistake."""
        def _do():
            already = bool(self._paused_reason)
            self._paused_reason = reason
            self._paused_until = float(resume_at or 0.0)
            self._resume.clear()
            self._paused_event.set()
            if not already:
                log.info("paused: %s (until %s)", reason, self._paused_until or "manual")
            self.bus.publish("office.paused", reason=reason,
                             resume_at=self._paused_until or None)
        self._on_loop(_do)

    def resume(self):
        def _do():
            if not self._paused_reason:
                return
            log.info("resumed")
            self._paused_reason, self._paused_until = "", 0.0
            self._paused_event.clear()
            self._resume.set()
            self.bus.publish("office.resumed")
        self._on_loop(_do)

    def paused_summary(self):
        if not self._paused_reason:
            return ""
        if self._paused_until:
            when = time.strftime("%H:%M", time.localtime(self._paused_until))
            return f"{self._paused_reason} (resumes about {when})"
        return self._paused_reason

    def cancel_task(self, task_id):
        """Stop a task now, wherever it is. Queued: it is skipped when its
        worker reaches it. Running: the worker's current job is cancelled -
        which ends the model call - and the worker takes the next one."""
        row = self.store.task(task_id)
        if row is None or row["status"] not in ("queued", "running"):
            return None
        def _do():
            self._cancelled.add(task_id)
            # "queued" in the database can still mean a worker holds it -
            # parked on a pause, or waiting for a model slot. If its job is
            # live, cancel the job; the worker's except branch settles the row.
            job = self._current.get(row["assignee"])
            if job is not None and not job.done() and job.get_name() == f"job:{task_id}":
                job.cancel()
                return
            self._finish_cancelled(row["assignee"], task_id)
        self._on_loop(_do)
        return row

    def _finish_cancelled(self, agent_id, task_id):
        self._cancelled.discard(task_id)
        self.store.update_task(task_id, status="cancelled", stop="cancelled",
                               error="cancelled by your principal",
                               finished_at=time.time())
        self.bus.publish("task.updated", agent_id=agent_id, task_id=task_id,
                         status="cancelled")
        event = self._task_events.pop(task_id, None)
        if event is not None:
            event.set()

    def _on_loop(self, fn):
        try:
            on_loop = asyncio.get_running_loop() is self.loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            fn()
        else:
            self.loop.call_soon_threadsafe(fn)

    async def _await_resume(self, agent_id, detail):
        """Park until the office is running again. Shows why on the floor."""
        if self._resume.is_set():
            return
        self._set_status(agent_id, "queued", f"paused: {detail}"[:80])
        await self._resume.wait()

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
        # A double-click on Send, or a retried POST, is one request, not two.
        digest = hashlib.sha1(f"{to}|{text}".encode()).hexdigest()
        last, at = self._last_message
        if digest == last and time.time() - at < 5:
            return {"duplicate": True}
        self._last_message = (digest, time.time())

        if to and to != config.MANAGER_ID and to in config.STAFF_IDS:
            self.store.add_message("user", to, text)
            self.bus.publish("user.message", text=text, to=to)
            asyncio.run_coroutine_threadsafe(self._route_to(to, text), self.loop)
            return {"to": to}
        self.store.add_message("user", config.MANAGER_ID, text)
        self.bus.publish("user.message", text=text)
        if config.ROUTER and to != config.MANAGER_ID and hasattr(self.backend, "structured"):
            asyncio.run_coroutine_threadsafe(self._front_desk(text), self.loop)
        else:
            self.loop.call_soon_threadsafe(self.queues[config.MANAGER_ID].put_nowait, text)
        return {"to": config.MANAGER_ID}

    async def _front_desk(self, text):
        """Classify, then answer / route / hand to Miles. Any doubt, any
        error, any low confidence: Miles, exactly as if the desk were not
        there."""
        now_text = (dt.datetime.now(config.TZ) if config.TZ
                    else dt.datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M %Z (%A)")
        async with self._desk_lock:
            await self._front_desk_locked(text, now_text)

    async def _front_desk_locked(self, text, now_text):
        decision, turn, ms = None, None, 0
        try:
            decision, turn, ms = await router.classify(self, text, now_text)
        except Exception:
            log.exception("front desk crashed; handing to the manager")
        if turn is not None:
            self.store.add_usage_row("router", config.MODEL_CHEAP, turn)
            self.bus.publish("usage", agent_id="router", cost=turn.cost_usd,
                             tokens=turn.input_tokens + turn.output_tokens
                             + turn.cache_read + turn.cache_write,
                             spend=self._spend_windows())
        action = (decision or {}).get("action", "manager")
        conf = (decision or {}).get("confidence", 0.0)
        if action != "manager" and conf < config.ROUTER_CONFIDENCE:
            action = "manager"
        if action == "answer":
            self._say_to_user(decision["reply"])
        elif action == "route":
            await self._route_to(decision["target"], text, via_desk=True)
        else:
            self.queues[config.MANAGER_ID].put_nowait(text)
        self.bus.publish("router.decided", action=action,
                         target=(decision or {}).get("target", ""),
                         confidence=round(conf, 2), ms=ms,
                         floor=config.ROUTER_CONFIDENCE)

    async def _route_to(self, agent_id, text, via_desk=False):
        """A request that skips Miles still gets its answer in chat. The
        direct 'To' path used to finish in silence: the task card was the
        only place the result existed."""
        title = " ".join(text.split())[:60] or "Direct request"
        try:
            task_id = await self.assign(agent_id, title, text, created_by="user")
        except KeyError:
            self.queues[config.MANAGER_ID].put_nowait(text)
            return
        asyncio.create_task(self._reply_when_done(task_id, agent_id, via_desk),
                            name=f"reply:{task_id}")

    async def _reply_when_done(self, task_id, agent_id, via_desk):
        event = self._task_events.get(task_id)
        if event is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), WAIT_TIMEOUT_S * 4)
        row = self.store.task(task_id)
        if row is None:
            return
        status = row["status"]
        if status == "done":
            text = row["result"] or "(no output)"
        elif status == "partial":
            text = (f"{row['result'] or ''}\n\n(I ran out of "
                    f"{(row['stop'] or 'room').replace('_', ' ')} before finishing.)").strip()
        elif status == "cancelled":
            return
        elif status in ("queued", "running"):
            text = "Still working on this - it has been a while. Check the task board."
        else:
            text = f"I couldn't finish this: {row['error'] or 'unknown error'}"
        self.store.add_message(agent_id, "user", text, task_id)
        self.bus.publish("agent.message_user", agent_id=agent_id, task_id=task_id,
                         text=text, routed=via_desk, status=status)

    def _spend_windows(self):
        now = time.time()
        return {"session": self.store.spend(now - config.SESSION_WINDOW_S),
                "day": self.store.spend(now - 86400), "total": self.store.spend()}

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
            # Also return the moment the office pauses: a task that cannot
            # start will not finish, and 900s of silence helps nobody.
            all_done = asyncio.ensure_future(asyncio.gather(*pending))
            paused = asyncio.ensure_future(self._paused_event.wait())
            try:
                await asyncio.wait({all_done, paused}, timeout=WAIT_TIMEOUT_S,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                for fut in (all_done, paused):
                    if not fut.done():
                        fut.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await fut
        self._set_status(requester, "working", "")
        return {t: (self.store.task(t) or {}) for t in task_ids}

    # -- human in the loop ----------------------------------------------
    async def request_approval(self, ctx, kind, action, detail=""):
        """Returns a Verdict: truthy when approved, and readable as
        "declined" or "expired" when not."""
        verdict, _ = await self._await_human(ctx, kind, action, detail)
        return verdict

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
            verdict = Verdict("approved" if approved else "declined")
        except asyncio.TimeoutError:
            self.store.decide_approval(approval_id, "expired")
            self.bus.publish("approval.decided", agent_id=ctx.agent_id,
                             id=approval_id, status="expired")
            verdict, response = Verdict("expired"), ""
        finally:
            self._approval_waiters.pop(approval_id, None)
            self._set_status(ctx.agent_id, "working", ctx.task_title[:60])
        return verdict, response

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
            if task_id in self._cancelled:            # cancelled while queued
                self._finish_cancelled(agent_id, task_id)
                queue.task_done()
                continue
            job = asyncio.create_task(self._run_task(role, task_id),
                                      name=f"job:{task_id}")
            self._current[agent_id] = job
            try:
                await job
            except asyncio.CancelledError:
                # Two very different cancellations arrive the same way: the
                # office shutting this worker down, or a human cancelling
                # one job. Only the second one is ours to absorb.
                if asyncio.current_task().cancelling():
                    raise
                self._release_slot_if_held(agent_id)
                self._finish_cancelled(agent_id, task_id)
            except Exception as exc:
                log.exception("worker %s crashed on %s", agent_id, task_id)
                self.store.update_task(task_id, status="failed", error=str(exc),
                                       finished_at=time.time())
                self.bus.publish("task.updated", agent_id=agent_id, task_id=task_id,
                                 status="failed")
                self._on_failure(agent_id, task_id, str(exc))
            finally:
                self._current.pop(agent_id, None)
                # Set, then forget. Anyone already awaiting holds their own
                # reference; a later `wait` finds no event and reads the
                # finished row directly. Keeping these grew without bound.
                event = self._task_events.pop(task_id, None)
                if event is not None:
                    event.set()
                self._set_status(agent_id, "idle", "")
                queue.task_done()

    def _settled_meanwhile(self, task_id):
        """True when someone finished this row while the worker was parked -
        a cancel during a pause, in practice."""
        row = self.store.task(task_id)
        return row is None or row["status"] not in ("queued", "running")

    def _release_slot_if_held(self, agent_id):
        # A job cancelled inside `async with self._model_slots` releases the
        # slot on the way out; nothing to do. Kept as a named hook so the
        # invariant has a place to live if _run_model ever changes.
        return None

    async def _run_task(self, role, task_id):
        task = self.store.task(task_id)
        if task is None or task["status"] not in ("queued", "running"):
            return
        # A budget ceiling or a rate limit pauses the office; the task waits
        # here, still queued, and runs when the pause lifts. It used to be
        # marked failed on the spot, which turned a spending cap into a row of
        # people summoned to the manager's office.
        while True:
            await self._await_resume(role.id, self._paused_reason)
            blocked = self._budget_block()
            if not blocked:
                break
            if not self._paused_reason:
                self.pause(blocked, self._budget_resume_at())
        if task_id in self._cancelled or self._settled_meanwhile(task_id):
            return

        self.store.update_task(task_id, status="running", started_at=time.time())
        # "queued" until _run_model actually holds a slot; then "working".
        self._set_status(role.id, "queued", task["title"][:60], task_id)
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
        while True:
            turn = await self._run_model(request, ctx)
            self._record_usage(role, turn, task_id)
            if turn.stop != "rate_limited":
                break
            # The backend said no, not the employee. Park the office, keep
            # the task, and try again when the window reopens.
            self._pause_for_rate_limit(turn)
            self.store.update_task(task_id, status="queued")
            self.bus.publish("task.updated", agent_id=role.id, task_id=task_id,
                             status="queued")
            await self._await_resume(role.id, self._paused_reason)
            if task_id in self._cancelled or self._settled_meanwhile(task_id):
                return
            self.store.update_task(task_id, status="running", started_at=time.time())
            self.bus.publish("task.updated", agent_id=role.id, task_id=task_id,
                             status="running")

        result = ctx.result or turn.text or "(no output)"
        # Three outcomes, not two. `finish` was called: done, whatever the
        # stop reason - the agent said it was finished. Otherwise a run cut
        # off by max_turns or the budget is *partial*: the office's cap did
        # its job, the text is whatever exists so far, and nobody gets
        # summoned to the manager's office for it. Only an error is a failure.
        if turn.error:
            status = "failed"
        elif not ctx.result and turn.stop in llm.PARTIAL_STOPS:
            status = "partial"
        else:
            status = "done"
        # The office records what happened itself. Paying an agent to summarise
        # what the database already knows would be an absurd way to spend a turn.
        with contextlib.suppress(Exception):
            notebook.log_activity(
                self.store,
                f"**{role.name}** {status} — {ctx.task_title}"
                + (f" ({turn.error.splitlines()[0][:70]})" if turn.error else
                   f" (stopped: {turn.stop})" if status == "partial" else ""))
        self.store.update_task(task_id, status=status, result=result,
                               error=turn.error or None, stop=turn.stop or None,
                               finished_at=time.time())
        self.bus.publish("task.updated", agent_id=role.id, task_id=task_id,
                         status=status, stop=turn.stop, result=result[:400])
        if status == "failed":
            self._on_failure(role.id, task_id, turn.error)

    async def _manager_loop(self):
        role = config.role(config.MANAGER_ID)
        queue = self.queues[config.MANAGER_ID]
        while True:
            text = await queue.get()
            try:
                await self._await_resume(role.id, self._paused_reason)
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
            if not self._paused_reason:
                self.pause(blocked, self._budget_resume_at())
            self._say_to_user(f"I can't start on that yet: {blocked}")
            return
        self._set_status(role.id, "thinking", "reading your request")
        ctx = AgentContext(self, role.id, None, "inbox")
        request = llm.RunRequest(
            agent_id=role.id,
            system=(config.fill(role.persona, self.principal())
                    + notebook.lesson_block(self.store, role.id)),
            prompt=self._memory_block(text) + text,
            tools=tools.specs_for(role),
            native_tools=role.native_tools,
            skills=role.skills,
            model=role.model_id,
            effort=role.effort,
            max_turns=role.max_turns,
            budget_usd=config.TASK_BUDGET_USD * 2,
            cwd=str(config.WORKSPACE),
        )
        turn = await self._run_model(request, ctx)
        self._record_usage(role, turn)
        if turn.stop == "rate_limited":
            self._pause_for_rate_limit(turn)
            self._say_to_user("The model is rate-limited right now, so the office is "
                              f"paused. I'll pick this up again about "
                              f"{time.strftime('%H:%M', time.localtime(self._paused_until))}.")
            # Put it back: the manager loop parks on resume before the next one.
            self.queues[config.MANAGER_ID].put_nowait(text)
            return
        # message_user is the manager's proper exit; fall back to raw text so a
        # reply is never silently swallowed - and a reply cut short says so,
        # rather than reading as a complete answer that happens to end oddly.
        if not ctx.result:
            text = turn.error or turn.text or ""
            if turn.stop in llm.PARTIAL_STOPS and not turn.error:
                why = {"max_turns": "turns", "budget_exhausted": "budget",
                       "max_tokens": "room to reply"}.get(turn.stop, turn.stop)
                text = (text + "\n\n" if text else "") + \
                       f"(I ran out of {why} before finishing. Ask me to continue.)"
            if text:
                self._say_to_user(text)

    # -- helpers ---------------------------------------------------------
    def _memory_block(self, current_text):
        """The last few exchanges, clipped, so a follow-up has something to
        follow. Goes in the user turn, not the system prompt, so the cached
        prefix stays byte-identical across requests."""
        n = config.MANAGER_MEMORY
        if not n:
            return ""
        rows = self.store.messages(limit=n * 2 + 1)
        # The message being handled was stored before this runs; drop it.
        if rows and rows[-1]["sender"] == "user" and rows[-1]["body"] == current_text:
            rows = rows[:-1]
        rows = [r for r in rows if r["sender"] in ("user", config.MANAGER_ID)][-(n * 2):]
        if not rows:
            return ""
        lines = []
        for r in rows:
            who = "You" if r["sender"] == "user" else "Miles"
            lines.append(f"{who}: {llm.truncate(' '.join(r['body'].split()), 220)}")
        return ("Recent conversation, oldest first, for context only - the new "
                "message is at the end:\n" + "\n".join(lines) + "\n\nNew message:\n")

    def _pause_for_rate_limit(self, turn):
        wait = turn.retry_after or config.RATE_LIMIT_PAUSE_S
        resume_at = time.time() + wait
        log.warning("rate limited by the backend (%s); pausing %ds",
                    (turn.error or "")[:200], wait)
        self.pause("the model reported a rate limit", resume_at)

    def _budget_resume_at(self):
        """When a budget block lifts on its own: the oldest row in the window
        ages out. Daily budget: 24h from the first spend; token ceiling: the
        session window from its first turn."""
        now = time.time()
        if config.SESSION_TOKEN_BUDGET:
            since = now - config.SESSION_WINDOW_S
            if self.store.usage_totals(since)["tokens"] >= config.SESSION_TOKEN_BUDGET:
                return (self.store.usage_first_ts(since) or now) + config.SESSION_WINDOW_S
        first = self.store.usage_first_ts(now - 86400) or now
        return first + 86400

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

    def _record_usage(self, role, turn, task_id=None):
        self.store.add_usage_row(role.id, role.model_id, turn, task_id)
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

    async def _run_model(self, request, ctx):
        """Every model call passes through one semaphore. On a subscription the
        limits are sized for one person typing; nine agents starting at once is
        how you meet the 5-hour window at 9:04am."""
        if self._model_slots.locked():
            self._set_status(ctx.agent_id, "queued", "waiting for a free model slot",
                             ctx.task_id)
        async with self._model_slots:
            self._set_status(ctx.agent_id, "working", (ctx.task_title or "")[:60],
                             ctx.task_id)
            return await self.backend.run(request, ctx, ctx.emit)

    def _budget_block(self):
        if self._paused_reason:
            return self._paused_reason
        now = time.time()
        spent = self.store.spend_since(now - 86400)["usd"]
        if config.DAILY_BUDGET_USD and spent >= config.DAILY_BUDGET_USD:
            reason = (f"Daily budget reached (${spent:.2f} of "
                      f"${config.DAILY_BUDGET_USD:.2f} in the last 24h). "
                      "The office is paused. Raise OFFICE_DAILY_BUDGET_USD or wait.")
            return reason
        # On a subscription the dollar figure is notional; tokens per window
        # are what the plan actually meters. This is the ceiling that counts.
        if config.SESSION_TOKEN_BUDGET:
            since = now - config.SESSION_WINDOW_S
            used = self.store.usage_totals(since)["tokens"]
            if used >= config.SESSION_TOKEN_BUDGET:
                first = self.store.usage_first_ts(since) or now
                resume = first + config.SESSION_WINDOW_S
                reason = (f"Token ceiling reached: {used:,} of "
                          f"{config.SESSION_TOKEN_BUDGET:,} in the last "
                          f"{config.SESSION_WINDOW_S // 3600}h. Paused until about "
                          f"{time.strftime('%H:%M', time.localtime(resume))}.")
                return reason
        return ""

    # -- background ticker -----------------------------------------------
    async def _ticker(self):
        """Fires due reminders and, once a day, prunes old rows. Never calls a
        model: an idle office costs nothing."""
        while True:
            await asyncio.sleep(5)
            try:
                self._tick()
            except Exception:
                log.exception("ticker failed")

    def _tick(self):
        """One pass of the ticker. A timed pause lifts itself here; a budget
        pause re-checks the ledger first and pushes its own deadline out if
        the window has not actually cleared."""
        if self._paused_reason and self._paused_until \
                and time.time() >= self._paused_until:
            if self._budget_block() and "rate limit" not in self._paused_reason:
                self._paused_until = self._budget_resume_at()
            else:
                self.resume()
        for row in self.store.due_reminders():
            self.store.mark_reminder_fired(row["id"])
            self._say_to_user(f"⏰ Reminder: {row['text']}")
        if time.time() - self._last_prune > 86400:
            self._last_prune = time.time()
            removed = self.store.prune(config.RETENTION_DAYS)
            if any(removed.values()):
                log.info("pruned %s", removed)


class Verdict(str):
    """What the human said to an approval: "approved", "declined" or
    "expired". Truthy only when approved, so `if ok:` keeps working; the
    string tells a tool which kind of no it got. An absent principal is not
    an opposed one, and an agent told "declined" on a timeout would report
    a refusal that never happened."""

    def __bool__(self):
        return str.__eq__(self, "approved")


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
