"""Incremental parsers turning each CLI's headless output into normalized events.

Normalized event: {"kind": init|text|tool|usage|result|error|limit|progress|other, ...}
Parsers are tolerant: unknown fields are ignored and missing metrics stay None (never fabricated).
Field names follow the vendors' documentation (Claude `stream-json`, Antigravity `stream-json`);
Copilot's JSONL schema is undocumented, so its parser is deliberately generic and its usage comes from OTel if present.
The raw stream is always archived by the runner so parsers can be corrected and replayed (see tests/fixtures/streams).
"""
from __future__ import annotations

import json
import re

LIMIT_RX = re.compile(r"rate.?limit|usage.?limit|quota|too many requests|\b429\b|limit reached|exceeded.*(limit|quota)|credits? (exhausted|limit)|max[- ]ai[- ]credits", re.I)


def _int(v):
    return int(v) if isinstance(v, (int, float)) else None


def classify_error(text: str | None) -> str | None:
    """Return 'limit' for usage/rate-limit style failures, else 'error' if text present."""
    if not text:
        return None
    return "limit" if LIMIT_RX.search(text) else "error"


# ---------------------------------------------------------------- Claude Code

def parse_claude(obj: dict) -> list[dict]:
    t, st = obj.get("type"), obj.get("subtype")
    out = []
    if t == "system" and st == "init":
        out.append({"kind": "init", "session": obj.get("session_id"), "model": obj.get("model")})
    elif t == "system" and st == "api_retry":
        err = obj.get("error")
        out.append({"kind": "limit" if err in ("rate_limit", "overloaded", "billing_error") else "error",
                    "detail": f"api_retry {err} status={obj.get('error_status')} attempt={obj.get('attempt')}",
                    "retry": True, "session": obj.get("session_id")})
    elif t == "assistant":
        msg = obj.get("message") or {}
        u = msg.get("usage") or {}
        if u:
            ctx = sum(x for x in (_int(u.get("input_tokens")), _int(u.get("cache_read_input_tokens")), _int(u.get("cache_creation_input_tokens"))) if x)
            out.append({"kind": "usage", "scope": "delta", "input": _int(u.get("input_tokens")), "output": _int(u.get("output_tokens")),
                        "cache_read": _int(u.get("cache_read_input_tokens")), "cache_create": _int(u.get("cache_creation_input_tokens")),
                        "context_tokens": ctx or None, "model": msg.get("model"), "msg_id": msg.get("id")})
        for c in msg.get("content") or []:
            if c.get("type") == "tool_use":
                out.append({"kind": "tool", "name": c.get("name"), "input": c.get("input")})
            elif c.get("type") == "text" and c.get("text"):
                out.append({"kind": "text", "text": c["text"][:300]})
    elif t == "result":
        u = obj.get("usage") or {}
        mu = obj.get("modelUsage") or {}
        ctxw = None
        for m in mu.values():
            if isinstance(m, dict) and m.get("contextWindow"):
                ctxw = _int(m["contextWindow"])
        err = obj.get("is_error") or (st and st != "success")
        out.append({"kind": "result", "ok": not err, "session": obj.get("session_id"), "text": (obj.get("result") or "")[:500],
                    "cost": obj.get("total_cost_usd"), "turns": obj.get("num_turns"), "context_size": ctxw,
                    "usage": {"input": _int(u.get("input_tokens")), "output": _int(u.get("output_tokens")),
                              "cache_read": _int(u.get("cache_read_input_tokens")), "cache_create": _int(u.get("cache_creation_input_tokens"))} if u else None,
                    "models": list(mu) or None,
                    "error": (obj.get("result") or st) if err else None})
        if err:
            out.append({"kind": classify_error(str(obj.get("result") or st)) or "error", "detail": str(obj.get("result") or st)[:300]})
    return out


# ---------------------------------------------------------------- Antigravity
# Verified against a real `agy -p --output-format stream-json` run (2026-11-02): events are NESTED:
#   {"event":"init","conversation_id":..,"init":{...}}  {"event":"step_update","step_update":{conversation_id,step_type,state,usage,..}}
#   {"event":"result","result":{conversation_id,status,response,usage,..}}
# step_update.usage is PER STEP (a delta); result.usage is the cumulative total (sum of step deltas == result total).

def parse_antigravity(obj: dict) -> list[dict]:
    ev = obj.get("event")
    out = []
    if ev == "init":
        i = obj.get("init") or {}
        out.append({"kind": "init", "session": obj.get("conversation_id") or i.get("conversation_id"), "model": i.get("model") or obj.get("model")})
    elif ev == "step_update":
        su = obj.get("step_update") or obj  # tolerate the flat shape shown in the docs
        cid = su.get("conversation_id")
        u = su.get("usage") or {}
        if u:
            out.append({"kind": "usage", "scope": "delta", "input": _int(u.get("input_tokens")), "output": _int(u.get("output_tokens")),
                        "thinking": _int(u.get("thinking_tokens")), "cache_read": _int(u.get("cache_read_tokens")),
                        "total": _int(u.get("total_tokens")), "session": cid})
        stype, state = su.get("step_type"), su.get("state")
        if stype == "tool":
            ti = su.get("tool_info") or {}
            err = (ti.get("error") or {}).get("message")
            if state == "ACTIVE":
                out.append({"kind": "tool", "name": su.get("tool_name") or ti.get("name"), "input": ti.get("parameters"), "session": cid})
            if err:
                out.append({"kind": "error", "detail": f"tool error: {err}"[:300], "session": cid})
        elif stype == "agent_response" and su.get("text_delta", "").strip():
            out.append({"kind": "text", "text": su["text_delta"][:300], "session": cid})
        elif stype == "checkpoint":
            out.append({"kind": "progress", "detail": "checkpoint step", "session": cid})
    elif ev == "result":
        r = obj.get("result") if isinstance(obj.get("result"), dict) else obj
        u = r.get("usage") or {}
        ok = r.get("status") == "SUCCESS"
        out.append({"kind": "result", "ok": ok, "session": r.get("conversation_id"), "text": (r.get("response") or "")[:500],
                    "turns": r.get("num_turns"), "cost": None,  # Antigravity exposes no cost
                    "usage": {"input": _int(u.get("input_tokens")), "output": _int(u.get("output_tokens")),
                              "thinking": _int(u.get("thinking_tokens")), "cache_read": _int(u.get("cache_read_tokens"))} if u else None,
                    "error": None if ok else (r.get("error") or r.get("status"))})
        if not ok:
            out.append({"kind": classify_error(str(r.get("error") or r.get("status"))) or "error", "detail": str(r.get("error") or r.get("status"))[:300]})
    return out


# ---------------------------------------------------------------- Copilot CLI
# Verified against a real `copilot -p --output-format json` run (2026-11-02). Events are {"type","data",...}:
#   tool.execution_start{toolName,arguments}, assistant.message{toolRequests}, session.usage_checkpoint{totalNanoAiu,totalPremiumRequests},
#   result{sessionId,exitCode,usage{premiumRequests,..}}  (no token counts in the stream; tokens come from the OTel export)
# The session id is only present on `result`.

def parse_copilot(obj: dict) -> list[dict]:
    t = str(obj.get("type") or "")
    d = obj.get("data") if isinstance(obj.get("data"), dict) else {}
    out = []
    if t == "tool.execution_start":
        out.append({"kind": "tool", "name": d.get("toolName"), "input": d.get("arguments")})
    elif t == "session.tools_updated":
        out.append({"kind": "init", "session": None, "model": d.get("model")})
    elif t == "session.usage_checkpoint":
        out.append({"kind": "usage", "scope": "cumulative", "nano_aiu": d.get("totalNanoAiu"), "premium_requests": d.get("totalPremiumRequests")})
    elif t == "tool.execution_complete" and d.get("success") is False:
        out.append({"kind": "error", "detail": f"tool failed: {str((d.get('result') or {}).get('content'))[:200]}"})
    elif t == "assistant.turn_start":
        out.append({"kind": "progress", "detail": "turn_start"})
    elif t == "result":
        code = obj.get("exitCode")
        ok = (code == 0) if code is not None else not obj.get("error")
        u = obj.get("usage") or {}
        out.append({"kind": "result", "ok": ok, "session": obj.get("sessionId"), "text": "", "cost": None, "turns": None,
                    "usage": None, "premium_requests": u.get("premiumRequests"), "error": None if ok else f"exit code {code}"})
        if not ok:
            out.append({"kind": "error", "detail": f"copilot exit code {code}"})
    if re.search(r"(^|\.)error$|\.failed$", t) or (obj.get("error") and t != "result"):
        text = d.get("message") or obj.get("error") or t
        text = text if isinstance(text, str) else json.dumps(text)
        out.append({"kind": classify_error(text) or "error", "detail": text[:300]})
    return out


PARSERS = {"claude": parse_claude, "antigravity": parse_antigravity, "copilot": parse_copilot}


def parse_line(cli: str, line: str) -> list[dict]:
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except ValueError:
        # plain text line: still check for limit wording so a text-mode failure is detected
        k = classify_error(line) if LIMIT_RX.search(line) else None
        return [{"kind": k, "detail": line[:300]}] if k else [{"kind": "other", "raw": line[:200]}]
    if not isinstance(obj, dict):
        return []
    return PARSERS.get(cli, lambda o: [])(obj)


def parse_otel_file(text: str) -> dict | None:
    """Copilot OTel JSONL export -> token and credit totals. Verified shape (2026-11-02): span records with
    attributes gen_ai.operation.name, gen_ai.usage.{input,output,cache_read.input}_tokens, github.copilot.nano_aiu, gen_ai.conversation.id.
    The `invoke_agent` span REPEATS the totals of its child `chat` spans, so use invoke_agent when present, else sum the chat spans
    (never both). Returns None unless token attributes are actually found."""
    chat, agent, conv = [], [], None
    for ln in text.splitlines():
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("type") != "span":
            continue
        a = o.get("attributes") or {}
        op = a.get("gen_ai.operation.name")
        conv = conv or a.get("gen_ai.conversation.id")
        if "gen_ai.usage.input_tokens" not in a and "gen_ai.usage.output_tokens" not in a:
            continue
        rec = (a.get("gen_ai.usage.input_tokens") or 0, a.get("gen_ai.usage.output_tokens") or 0,
               a.get("gen_ai.usage.cache_read.input_tokens") or 0, a.get("github.copilot.nano_aiu") or 0)
        (agent if op == "invoke_agent" else chat if op == "chat" else []).append(rec)
    rows = agent or chat
    if not rows:
        return None
    return {"input": sum(r[0] for r in rows), "output": sum(r[1] for r in rows), "cache_read": sum(r[2] for r in rows),
            "nano_aiu": sum(r[3] for r in rows) or None, "session": conv, "source": "invoke_agent" if agent else "chat"}
