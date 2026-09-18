"""Swappable LLM backends.

Three implementations behind one `run_agent()` call:

  agentsdk - Claude Agent SDK. Bundles its own Claude Code binary, so no Node
             install is needed. Uses whatever credentials the environment
             already holds: CLAUDE_CODE_OAUTH_TOKEN for a Claude subscription,
             ANTHROPIC_API_KEY for pay-as-you-go API credits.
  api      - Anthropic Messages API directly, with a hand-rolled tool loop.
  mock     - No network, no spend. Plausible behaviour so the GUI can be
             developed and demoed for free.

Switch with OFFICE_BACKEND. Nothing else in the codebase knows which is active.

Token economy notes are marked BUDGET. The short version: we send a bare
system prompt (never the Claude Code preset), load no filesystem settings,
expose the smallest possible tool surface per role, cap turns, cap tool result
size, and start every task from a clean context instead of resuming a session.
"""

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field

from . import config

log = logging.getLogger("office.llm")


@dataclass
class Turn:
    """What one agent turn produced."""
    text: str = ""
    stop: str = "end_turn"
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    error: str = ""
    turns: int = 0            # model calls the run took (SDK: num_turns)
    api_status: int = 0       # HTTP status of a failing API call, else 0
    retry_after: float = 0.0  # seconds the backend asked us to wait, if it said


# A rate limit is the office's problem, not the employee's: nine tasks failing
# at once with "429" used to read as nine people being called into the
# manager's office. Detection lives here, in one place, for every backend.
# On the agentsdk backend the only signal is the CLI's prose, so the raw text
# is logged on every match - a false positive must be diagnosable.
RATE_LIMIT_PATTERNS = re.compile(
    r"rate.?limit|too many requests|\b429\b|\b529\b|overloaded|usage limit|"
    r"quota|capacity|hit your limit|limit reached", re.I)


# Ways a run can end that are the office's own caps, not the agent's fault.
# A task that stops on one of these is *partial*: it holds whatever was
# produced so far, and the manager is told it was cut off, not finished.
PARTIAL_STOPS = ("max_turns", "budget_exhausted", "max_tokens")


def _sdk_stop(subtype="", terminal_reason="", stop_reason="", api_status=0,
              is_error=False):
    """One stop reason from the SDK's three overlapping fields.

    The CLI reports a max_turns or budget cut-off as an *error* result
    (subtype error_max_turns / error_max_budget_usd, is_error true) and then
    exits non-zero. Read literally, every task that hit its cap looked like
    a crash. Here those become plain stop reasons; only the rest are errors.
    """
    subtype, terminal_reason = subtype or "", terminal_reason or ""
    if subtype == "error_max_turns" or terminal_reason == "max_turns":
        return "max_turns"
    if subtype == "error_max_budget_usd":
        return "budget_exhausted"
    if terminal_reason.startswith("aborted"):
        return "cancelled"
    if api_status in (429, 529):
        return "rate_limited"
    if api_status:
        return "api_error"
    if subtype.startswith("error"):
        return subtype
    if is_error:
        return "error"
    if stop_reason and stop_reason not in ("end_turn", "stop_sequence"):
        return stop_reason                      # e.g. max_tokens
    return "end_turn"


def _sampling_kwargs(model, effort):
    """Per-model sampling controls for the Messages API.

    Haiku rejects `output_config.effort` and adaptive thinking - a 400 on
    every call, which used to break every role on the cheap model. It gets a
    fixed thinking budget sized by effort instead, and none at all on low,
    which is what a cheap role usually wants. max_tokens must exceed the
    thinking budget, so it grows with it.
    """
    if "haiku" in (model or ""):
        budget = {"medium": 2048, "high": 6144}.get(effort, 0)
        if not budget:
            return {"max_tokens": 4000}
        return {"max_tokens": 4000 + budget,
                "thinking": {"type": "enabled", "budget_tokens": budget}}
    return {"max_tokens": 4000,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort}}


@dataclass
class RunRequest:
    agent_id: str
    system: str
    prompt: str
    tools: list = field(default_factory=list)      # list[ToolSpec]
    native_tools: tuple = ()
    skills: tuple = ()
    model: str = ""
    effort: str = "low"
    max_turns: int = 8
    budget_usd: float = 0.0
    cwd: str = ""


class BackendError(RuntimeError):
    pass


def truncate(text, limit=None):
    """Clip text to `limit`, keeping the head and tail of long content.

    Middle-out rather than a plain cut: the end of a stack trace or a command's
    last lines usually matter as much as the first.
    """
    limit = limit or config.MAX_TOOL_RESULT_CHARS
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    if limit < 200:  # too short for a head/tail split to be worth the marker
        return text[: max(1, limit - 1)].rstrip() + "…"
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.25):]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n...[{dropped} chars truncated]...\n{tail}"


# ---------------------------------------------------------------------------
# Agent SDK backend
# ---------------------------------------------------------------------------

class AgentSDKBackend:
    name = "agentsdk"

    def __init__(self):
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise BackendError(
                "claude-agent-sdk is not installed. Run: pip install claude-agent-sdk"
            ) from exc
        self._sdk = __import__("claude_agent_sdk")

    def describe_auth(self):
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "claude subscription (CLAUDE_CODE_OAUTH_TOKEN)"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "anthropic api key"
        return "inherited claude code login (none found in env)"

    def _build_server(self, specs, ctx):
        """Wrap our neutral ToolSpecs as in-process MCP tools."""
        sdk = self._sdk
        sdk_tools = []
        for spec in specs:
            sdk_tools.append(self._wrap(sdk, spec, ctx))
        if not sdk_tools:
            return None
        return sdk.create_sdk_mcp_server(name="office", version="1.0.0", tools=sdk_tools)

    @staticmethod
    def _wrap(sdk, spec, ctx):
        async def handler(args):
            try:
                out = await spec.handler(args, ctx)
            except Exception as exc:  # a tool crash must not kill the agent
                return {
                    "content": [{"type": "text", "text": f"tool error: {exc}"}],
                    "is_error": True,
                }
            return {"content": [{"type": "text", "text": truncate(out)}]}

        handler.__name__ = f"office_{spec.name}"
        annotations = None
        if getattr(sdk, "ToolAnnotations", None):
            annotations = sdk.ToolAnnotations(
                readOnlyHint=spec.read_only,
                destructiveHint=not spec.read_only,
                # Not a clamp. This is the size up to which Claude Code keeps
                # a tool result inline instead of spilling it to a file and
                # showing a preview. It is set to the figure truncate() already
                # enforces in the handler above, so that path is never taken.
                maxResultSizeChars=config.MAX_TOOL_RESULT_CHARS,
            )
        return sdk.tool(spec.name, spec.description, spec.schema,
                        annotations=annotations)(handler)

    async def run(self, req, ctx, on_event):
        sdk = self._sdk
        server = self._build_server(req.tools, ctx)

        allowed = [f"mcp__office__{s.name}" for s in req.tools]
        # Read-only natives are pre-approved; anything that can touch the
        # machine or the network with side effects falls through to
        # can_use_tool and becomes an approval request in the GUI.
        for nt in req.native_tools:
            if nt in ("Read", "Grep", "Glob", "WebSearch", "WebFetch"):
                allowed.append(nt)

        # No env= here on purpose. The SDK merges options.env OVER os.environ
        # (subprocess_cli.py: {**inherited_env, **options.env}), so passing a
        # scrubbed dict withholds nothing. The daemon scrubs its own
        # environment at startup instead; see config.scrub_process_environment.
        options = sdk.ClaudeAgentOptions(
            # BUDGET: a bare string, never {"preset": "claude_code"} - the
            # preset is thousands of tokens of coding-agent instructions we
            # neither need nor want to pay for on every single task.
            system_prompt=req.system,
            # BUDGET: [] means do not read ~/.claude or ./.claude. Keeps
            # CLAUDE.md and user settings out of the context window entirely.
            #
            # Skills need their source directories, and the SDK widens this
            # itself when `skills` is set - so leave it alone rather than
            # guessing at a wider set than it would have chosen. The cost of
            # that widening is real and worth knowing: an employee holding a
            # skill also picks up ~/.claude/settings.json, and an `allow` rule
            # there (e.g. Bash) would auto-approve calls before this office's
            # approval gate is ever consulted. Grant skills accordingly.
            **({} if req.skills else {"setting_sources": []}),
            skills=list(req.skills) if req.skills else None,
            tools=list(req.native_tools) or [],
            allowed_tools=allowed,
            mcp_servers={"office": server} if server else {},
            model=req.model or None,
            # A plan that does not include opus should degrade a role to the
            # office default rather than fail every task that role ever gets.
            fallback_model=(config.MODEL_SMART
                            if req.model and req.model != config.MODEL_SMART else None),
            effort=req.effort,
            max_turns=req.max_turns,
            max_budget_usd=req.budget_usd or None,
            permission_mode="default",
            can_use_tool=ctx.permission_callback,
            cwd=req.cwd or str(config.WORKSPACE),
            include_partial_messages=False,
            stderr=lambda line: ctx.log_stderr(line),
        )

        turn = Turn()
        texts = []
        try:
            async for message in sdk.query(prompt=req.prompt, options=options):
                kind = type(message).__name__
                if kind == "AssistantMessage":
                    for block in getattr(message, "content", []) or []:
                        btype = type(block).__name__
                        if btype == "TextBlock" and getattr(block, "text", ""):
                            texts.append(block.text)
                            on_event("say", text=block.text)
                        elif btype == "ThinkingBlock":
                            thought = getattr(block, "thinking", "")
                            if thought:
                                on_event("thinking", text=thought)
                        elif btype == "ToolUseBlock":
                            on_event("tool", tool=_short_tool(block.name),
                                     args=_short_args(getattr(block, "input", {})))
                elif kind == "ResultMessage":
                    is_error = bool(getattr(message, "is_error", False))
                    api_status = getattr(message, "api_error_status", 0) or 0
                    turn.stop = _sdk_stop(
                        subtype=getattr(message, "subtype", ""),
                        terminal_reason=getattr(message, "terminal_reason", ""),
                        stop_reason=getattr(message, "stop_reason", ""),
                        api_status=api_status, is_error=is_error)
                    turn.turns = getattr(message, "num_turns", 0) or 0
                    turn.api_status = api_status
                    if is_error and turn.stop not in PARTIAL_STOPS:
                        errs = [str(e) for e in (getattr(message, "errors", None) or []) if e]
                        turn.error = truncate("; ".join(errs)
                                              or getattr(message, "result", None)
                                              or turn.stop, 600)
                    # ResultMessage carries total_cost_usd directly, and `usage`
                    # is a plain dict - not a nested cost object with attributes.
                    # Reading it the other way silently recorded zeroes for
                    # every turn, which left the spend counter permanently $0.00
                    # and the daily budget guard unable to ever fire.
                    turn.cost_usd = getattr(message, "total_cost_usd", None) or 0.0
                    usage = getattr(message, "usage", None)
                    if usage:
                        pull = (usage.get if isinstance(usage, dict)
                                else lambda k, d=0: getattr(usage, k, d))
                        turn.input_tokens = pull("input_tokens", 0) or 0
                        turn.output_tokens = pull("output_tokens", 0) or 0
                        turn.cache_read = pull("cache_read_input_tokens", 0) or 0
                        turn.cache_write = pull("cache_creation_input_tokens", 0) or 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # After an error result the CLI exits non-zero and the SDK raises
            # ResultError carrying that same result. Everything worth knowing
            # was read off the ResultMessage above; the exception confirms it.
            # For a cut-off there is nothing to add - and nothing to blame.
            result_error = (getattr(sdk, "ResultError", None)
                            or getattr(getattr(sdk, "_errors", None), "ResultError", None))
            if result_error is not None and isinstance(exc, result_error):
                if turn.stop == "end_turn":
                    turn.stop = _sdk_stop(subtype=exc.subtype,
                                          terminal_reason=exc.terminal_reason,
                                          api_status=exc.api_error_status or 0,
                                          is_error=True)
                turn.api_status = exc.api_error_status or turn.api_status
                if turn.stop not in PARTIAL_STOPS and not turn.error:
                    turn.error = truncate(
                        "; ".join(e for e in exc.errors if e) or exc.result or str(exc), 600)
            else:
                turn.error = f"{type(exc).__name__}: {exc}"

        if turn.error and turn.stop != "rate_limited" \
                and RATE_LIMIT_PATTERNS.search(turn.error):
            log.info("rate limit inferred from CLI text: %r", turn.error[:300])
            turn.stop = "rate_limited"
        if turn.error:
            on_event("error", text=turn.error)
        elif turn.stop in PARTIAL_STOPS:
            on_event("say", text=f"(stopped early: {turn.stop.replace('_', ' ')})")

        turn.text = "\n\n".join(t.strip() for t in texts if t.strip())
        return turn

    async def structured(self, system, prompt, schema, model=""):
        """One cheap, tool-less call that must answer in `schema`. Used by the
        front desk. Returns (data or None, Turn) - usage is the caller's to
        record."""
        sdk = self._sdk
        options = sdk.ClaudeAgentOptions(
            system_prompt=system,
            setting_sources=[],
            tools=[],
            allowed_tools=[],
            model=model or config.MODEL_CHEAP,
            fallback_model=config.MODEL_SMART,
            max_turns=1,
            output_format={"type": "json_schema", "schema": schema},
            permission_mode="default",
            cwd=str(config.WORKSPACE),
            include_partial_messages=False,
        )
        turn, data = Turn(), None
        try:
            async for message in sdk.query(prompt=prompt, options=options):
                if type(message).__name__ != "ResultMessage":
                    continue
                data = getattr(message, "structured_output", None)
                if data is None and getattr(message, "result", None):
                    with contextlib.suppress(ValueError, TypeError):
                        data = json.loads(message.result)
                turn.cost_usd = getattr(message, "total_cost_usd", None) or 0.0
                turn.turns = getattr(message, "num_turns", 0) or 0
                usage = getattr(message, "usage", None) or {}
                pull = (usage.get if isinstance(usage, dict)
                        else lambda k, d=0: getattr(usage, k, d))
                turn.input_tokens = pull("input_tokens", 0) or 0
                turn.output_tokens = pull("output_tokens", 0) or 0
                turn.cache_read = pull("cache_read_input_tokens", 0) or 0
                turn.cache_write = pull("cache_creation_input_tokens", 0) or 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            turn.error = f"{type(exc).__name__}: {exc}"
            if RATE_LIMIT_PATTERNS.search(turn.error):
                turn.stop = "rate_limited"
        return (data if isinstance(data, dict) else None), turn


def _short_tool(name):
    return name.rsplit("__", 1)[-1] if name else "tool"


def _short_args(args):
    if not isinstance(args, dict):
        return ""
    for key in ("command", "query", "file_path", "path", "text", "question", "title"):
        if key in args and args[key]:
            return truncate(str(args[key]), 160)
    return truncate(json.dumps(args, default=str), 120) if args else ""


# ---------------------------------------------------------------------------
# Anthropic Messages API backend
# ---------------------------------------------------------------------------

class APIBackend:
    name = "api"

    _ALIASES = {
        "opus": "claude-opus-5",
        "sonnet": "claude-sonnet-5",
        "haiku": "claude-haiku-4-5",
    }

    def __init__(self):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise BackendError(
                "anthropic is not installed. Run: pip install anthropic"
            ) from exc
        self._anthropic = anthropic
        self.client = anthropic.Anthropic()

    def describe_auth(self):
        return "anthropic api key" if os.environ.get("ANTHROPIC_API_KEY") else "ant profile / default credentials"

    def _model(self, alias):
        return self._ALIASES.get(alias, alias or "claude-sonnet-5")

    def _tool_defs(self, specs, native):
        defs = []
        for s in specs:
            defs.append({
                "name": s.name,
                "description": s.description,
                "input_schema": _json_schema(s.schema),
            })
        if "WebSearch" in native or "WebFetch" in native:
            # Server-side tool; runs on Anthropic's infrastructure.
            defs.append({"type": "web_search_20260209", "name": "web_search",
                         "max_uses": 4})
        return defs

    async def run(self, req, ctx, on_event):
        return await asyncio.to_thread(self._run_sync, req, ctx, on_event)

    def _run_sync(self, req, ctx, on_event):
        anthropic = self._anthropic
        model = self._model(req.model)
        specs = {s.name: s for s in req.tools}
        tools = self._tool_defs(req.tools, req.native_tools)

        # BUDGET: a stable cached prefix. system + tools are identical for
        # every task this role runs, so they are served from cache at ~10%
        # of input price after the first call.
        system = [{"type": "text", "text": req.system,
                   "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        messages = [{"role": "user", "content": req.prompt}]
        turn = Turn()
        texts = []
        spent = 0.0

        for _ in range(req.max_turns):
            try:
                resp = self.client.messages.create(
                    model=model,
                    system=system,
                    messages=messages,
                    tools=tools or anthropic.NOT_GIVEN,
                    **_sampling_kwargs(model, req.effort),
                )
            except anthropic.NotFoundError as exc:
                fallback = self._model(config.MODEL_SMART)
                if model != fallback:
                    on_event("say", text=f"{model} is not available here; "
                                          f"continuing on {fallback}.")
                    model = fallback
                    continue                       # costs one loop iteration, fine
                turn.error = f"model unavailable: {exc}"
                break
            except anthropic.RateLimitError as exc:
                turn.error = f"rate limited: {exc}"
                turn.stop = "rate_limited"
                turn.api_status = 429
                turn.retry_after = _retry_after(exc)
                break
            except anthropic.APIStatusError as exc:
                turn.error = f"api error {exc.status_code}: {exc.message}"
                turn.api_status = exc.status_code or 0
                if exc.status_code == 529:          # overloaded: same treatment
                    turn.stop = "rate_limited"
                    turn.retry_after = _retry_after(exc)
                break
            except anthropic.APIConnectionError as exc:
                turn.error = f"connection error: {exc}"
                break

            u = resp.usage
            turn.input_tokens += getattr(u, "input_tokens", 0) or 0
            turn.output_tokens += getattr(u, "output_tokens", 0) or 0
            turn.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
            turn.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
            spent = _estimate_cost(model, turn)

            if resp.stop_reason == "refusal":
                detail = getattr(resp, "stop_details", None)
                turn.error = f"refused ({getattr(detail, 'category', 'unknown')})"
                break

            for block in resp.content:
                if block.type == "text" and block.text:
                    texts.append(block.text)
                    on_event("say", text=block.text)
                elif block.type == "thinking" and getattr(block, "thinking", ""):
                    on_event("thinking", text=block.thinking)

            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            if resp.stop_reason != "tool_use":
                turn.stop = resp.stop_reason or "end_turn"
                break
            if req.budget_usd and spent > req.budget_usd:
                turn.stop = "budget_exhausted"
                break

            calls = [b for b in resp.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for call in calls:
                on_event("tool", tool=call.name, args=_short_args(call.input))
                spec = specs.get(call.name)
                if spec is None:
                    results.append({"type": "tool_result", "tool_use_id": call.id,
                                    "content": f"unknown tool {call.name}",
                                    "is_error": True})
                    continue
                try:
                    out = ctx.run_coroutine(spec.handler(call.input, ctx))
                    results.append({"type": "tool_result", "tool_use_id": call.id,
                                    "content": truncate(out)})
                except Exception as exc:
                    results.append({"type": "tool_result", "tool_use_id": call.id,
                                    "content": f"tool error: {exc}", "is_error": True})
            messages.append({"role": "user", "content": results})
        else:
            turn.stop = "max_turns"

        turn.text = "\n\n".join(t.strip() for t in texts if t.strip())
        turn.cost_usd = _estimate_cost(model, turn)
        return turn


    async def structured(self, system, prompt, schema, model=""):
        return await asyncio.to_thread(self._structured_sync, system, prompt, schema, model)

    def _structured_sync(self, system, prompt, schema, model):
        anthropic = self._anthropic
        model = self._model(model or config.MODEL_CHEAP)
        turn, data = Turn(), None
        try:
            resp = self.client.messages.create(
                model=model, max_tokens=400,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
                messages=[{"role": "user", "content": prompt}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            u = resp.usage
            turn.input_tokens = getattr(u, "input_tokens", 0) or 0
            turn.output_tokens = getattr(u, "output_tokens", 0) or 0
            turn.cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
            turn.cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
            turn.turns = 1
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            with contextlib.suppress(ValueError, TypeError):
                data = json.loads(text)
        except anthropic.RateLimitError as exc:
            turn.error, turn.stop = f"rate limited: {exc}", "rate_limited"
            turn.retry_after = _retry_after(exc)
        except Exception as exc:
            turn.error = f"{type(exc).__name__}: {exc}"
        turn.cost_usd = _estimate_cost(model, turn)
        return (data if isinstance(data, dict) else None), turn


def _retry_after(exc):
    """Seconds from a Retry-After header, if the SDK exposed one."""
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else 0.0
    except Exception:
        return 0.0


def _json_schema(simple):
    """Our ToolSpec.schema uses the SDK's simple {name: type} form; the
    Messages API needs real JSON Schema."""
    if isinstance(simple, dict) and simple.get("type") == "object":
        return simple
    py_to_json = {str: "string", int: "integer", float: "number", bool: "boolean",
                  list: "array", dict: "object"}
    props, required = {}, []
    for key, typ in (simple or {}).items():
        props[key] = {"type": py_to_json.get(typ, "string")}
        required.append(key)
    return {"type": "object", "properties": props, "required": required}


def _estimate_cost(model, turn):
    pin, pout = config.PRICING.get(model, (2.0, 10.0))
    return round((
        turn.input_tokens * pin
        + turn.cache_read * pin * config.CACHE_READ_DISCOUNT
        + turn.cache_write * pin * config.CACHE_WRITE_MULTIPLIER
        + turn.output_tokens * pout
    ) / 1_000_000, 6)


# ---------------------------------------------------------------------------
# Mock backend
# ---------------------------------------------------------------------------

class MockBackend:
    name = "mock"

    # Some tasks fail on purpose. Without failures you never see the approval
    # queue or the manager's office get used, which is half the point of a
    # free demo mode. Set to 0 for a suspiciously harmonious workplace.
    FAILURE_RATE = 0.18

    def __init__(self):
        # Read when built, not when imported: the value must follow the
        # environment of the process that starts the office, not of whoever
        # imported this module first.
        self.FAILURE_RATE = float(os.environ.get("OFFICE_MOCK_FAILURE_RATE", "0.18"))

    EXCUSES = (
        "connection refused talking to the staging host",
        "the playbook referenced an inventory group that no longer exists",
        "no credentials for that cluster",
        "the build log was rotated before I could read it",
        "ran out of turns before reaching a conclusion",
    )

    def describe_auth(self):
        return "none (mock)"

    async def run(self, req, ctx, on_event):
        turn = Turn(cost_usd=0.0)
        on_event("thinking", text="Working out how to approach this.")
        await asyncio.sleep(random.uniform(0.6, 1.6))

        if req.agent_id == config.MANAGER_ID:
            turn.text = await self._mock_manager(req, ctx, on_event)
        else:
            names = [s.name for s in req.tools if s.read_only] or None
            if names:
                on_event("tool", tool=random.choice(names), args="")
                await asyncio.sleep(random.uniform(0.4, 1.0))
            # Now and then, check something with a colleague - so the demo
            # shows the walk over, the answer, and the note in the result.
            specs = {s.name: s for s in req.tools}
            others = [i for i in config.STAFF_IDS if i != req.agent_id]
            if "ask_colleague" in specs and others and random.random() < 0.3:
                who = random.choice(others)
                on_event("tool", tool="ask_colleague", args=who)
                await specs["ask_colleague"].handler(
                    {"employee": who, "question": "Anything I should know before "
                     f"I finish '{truncate(req.prompt, 40)}'?"}, ctx)
            if random.random() < self.FAILURE_RATE:
                excuse = random.choice(self.EXCUSES)
                if excuse.startswith("ran out of turns"):
                    # The office's cap, not a failure: a partial result.
                    turn.stop = "max_turns"
                    turn.text = (f"[mock] {config.role(req.agent_id).name} got halfway: "
                                 f"{truncate(req.prompt, 100)}")
                    on_event("say", text="(stopped early: max turns)")
                else:
                    turn.error = excuse
                    on_event("error", text=turn.error)
            else:
                turn.text = (f"[mock] {config.role(req.agent_id).name} handled: "
                             f"{truncate(req.prompt, 160)}")
        turn.input_tokens = random.randint(400, 900)
        turn.output_tokens = random.randint(80, 260)
        return turn

    async def structured(self, system, prompt, schema, model=""):
        """A keyword front desk: enough to show every path in the demo."""
        await asyncio.sleep(random.uniform(0.2, 0.5))
        turn = Turn(input_tokens=random.randint(300, 500), output_tokens=random.randint(20, 60),
                    turns=1)
        text = prompt.rsplit("Message:", 1)[-1].strip().lower()
        words = text.split()
        if text.startswith(("hi", "hello", "thanks", "thank you")) and len(words) <= 6:
            return {"action": "answer", "target": "", "confidence": 0.95,
                    "reply": "[mock] Hello! Send me anything and I'll get it to the right person."}, turn
        if " and " in text or " then " in text or " same " in text or " that " in text:
            return {"action": "manager", "target": "", "confidence": 0.9, "reply": ""}, turn
        for r in config.ROSTER:
            if r.id == config.MANAGER_ID:
                continue
            if r.name.lower() in text or r.id in text:
                return {"action": "route", "target": r.id, "confidence": 0.92, "reply": ""}, turn
        return {"action": "manager", "target": "", "confidence": 0.6, "reply": ""}, turn

    async def _mock_manager(self, req, ctx, on_event):
        specs = {s.name: s for s in req.tools}
        # The prompt carries the recent conversation ahead of the message;
        # a real manager writes its own brief, the mock just echoes the ask.
        ask = req.prompt.rsplit("New message:\n", 1)[-1].strip()
        if "assign" in specs and random.random() < 0.85:
            target = random.choice(config.STAFF_IDS)
            on_event("tool", tool="assign", args=target)
            await specs["assign"].handler(
                {"assignee": target, "title": truncate(ask, 40), "brief": ask}, ctx)
            if "wait" in specs:
                on_event("tool", tool="wait", args="")
                await specs["wait"].handler({"task_ids": ""}, ctx)
        return "[mock] Delegated and reported back."


# ---------------------------------------------------------------------------

_BACKEND = None


def get_backend():
    global _BACKEND
    if _BACKEND is None:
        if config.BACKEND == "mock":
            _BACKEND = MockBackend()
        elif config.BACKEND == "api":
            _BACKEND = APIBackend()
        else:
            _BACKEND = AgentSDKBackend()
    return _BACKEND
