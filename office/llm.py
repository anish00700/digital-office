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
import json
import os
import random
import re
import time
from dataclasses import dataclass, field

from . import config


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


@dataclass
class RunRequest:
    agent_id: str
    system: str
    prompt: str
    tools: list = field(default_factory=list)      # list[ToolSpec]
    native_tools: tuple = ()
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
                # BUDGET: keeps oversized results out of the context window.
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

        options = sdk.ClaudeAgentOptions(
            # BUDGET: a bare string, never {"preset": "claude_code"} - the
            # preset is thousands of tokens of coding-agent instructions we
            # neither need nor want to pay for on every single task.
            system_prompt=req.system,
            # BUDGET: [] means do not read ~/.claude or ./.claude. Keeps
            # CLAUDE.md and user settings out of the context window entirely.
            setting_sources=[],
            tools=list(req.native_tools) or [],
            allowed_tools=allowed,
            mcp_servers={"office": server} if server else {},
            model=req.model or None,
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
                    turn.stop = getattr(message, "terminal_reason", "") or "stop"
                    cost = getattr(message, "cost", None)
                    if cost is not None:
                        turn.cost_usd = getattr(cost, "total_cost_usd", 0.0) or 0.0
                    usage = getattr(message, "usage", None)
                    if usage is not None:
                        turn.input_tokens = getattr(usage, "input_tokens", 0) or 0
                        turn.output_tokens = getattr(usage, "output_tokens", 0) or 0
                        turn.cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                        turn.cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            turn.error = f"{type(exc).__name__}: {exc}"
            on_event("error", text=turn.error)

        turn.text = "\n\n".join(t.strip() for t in texts if t.strip())
        return turn


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
                    max_tokens=4000,
                    system=system,
                    messages=messages,
                    tools=tools or anthropic.NOT_GIVEN,
                    thinking={"type": "adaptive"},
                    output_config={"effort": req.effort},
                )
            except anthropic.RateLimitError as exc:
                turn.error = f"rate limited: {exc}"
                break
            except anthropic.APIStatusError as exc:
                turn.error = f"api error {exc.status_code}: {exc.message}"
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

    def describe_auth(self):
        return "none (mock)"

    async def run(self, req, ctx, on_event):
        turn = Turn(cost_usd=0.0)
        on_event("thinking", text="Working out how to approach this.")
        await asyncio.sleep(random.uniform(0.6, 1.6))

        if req.agent_id == config.MANAGER_ID:
            text = await self._mock_manager(req, ctx, on_event)
        else:
            names = [s.name for s in req.tools if s.read_only] or None
            if names:
                pick = random.choice(names)
                on_event("tool", tool=pick, args="")
                await asyncio.sleep(random.uniform(0.4, 1.0))
            text = (f"[mock] {config.role(req.agent_id).name} handled: "
                    f"{truncate(req.prompt, 160)}")
        turn.text = text
        turn.input_tokens = random.randint(400, 900)
        turn.output_tokens = random.randint(80, 260)
        return turn

    async def _mock_manager(self, req, ctx, on_event):
        specs = {s.name: s for s in req.tools}
        if "assign" in specs and random.random() < 0.85:
            target = random.choice(config.STAFF_IDS)
            on_event("tool", tool="assign", args=target)
            await specs["assign"].handler(
                {"assignee": target, "title": truncate(req.prompt, 40),
                 "brief": req.prompt}, ctx)
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
