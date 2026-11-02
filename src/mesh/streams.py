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

def parse_antigravity(obj: dict) -> list[dict]:
    ev = obj.get("event")
    out = []
    if ev == "init":
        out.append({"kind": "init", "session": obj.get("conversation_id"), "model": obj.get("model")})
    elif ev == "step_update":
        cid = obj.get("conversation_id")
        u = obj.get("usage") or {}
        if u:
            out.append({"kind": "usage", "scope": "cumulative", "input": _int(u.get("input_tokens")), "output": _int(u.get("output_tokens")),
                        "thinking": _int(u.get("thinking_tokens")), "cache_read": _int(u.get("cache_read_tokens")),
                        "total": _int(u.get("total_tokens")), "session": cid})
        stype = obj.get("step_type")
        if stype == "tool":
            ti = obj.get("tool_info") or {}
            out.append({"kind": "tool", "name": obj.get("tool_name") or ti.get("name"), "input": ti.get("parameters"),
                        "error": (ti.get("error") or {}).get("message"), "session": cid})
        elif stype == "agent_response" and obj.get("text_delta"):
            out.append({"kind": "text", "text": obj["text_delta"][:300], "session": cid})
        elif stype == "checkpoint":
            out.append({"kind": "progress", "detail": "checkpoint step", "session": cid})
    elif ev == "result":
        u = obj.get("usage") or {}
        ok = obj.get("status") == "SUCCESS"
        out.append({"kind": "result", "ok": ok, "session": obj.get("conversation_id"), "text": (obj.get("response") or "")[:500],
                    "turns": obj.get("num_turns"), "cost": None,  # Antigravity exposes no cost
                    "usage": {"input": _int(u.get("input_tokens")), "output": _int(u.get("output_tokens")),
                              "thinking": _int(u.get("thinking_tokens")), "cache_read": _int(u.get("cache_read_tokens"))} if u else None,
                    "error": None if ok else (obj.get("error") or obj.get("status"))})
        if not ok:
            out.append({"kind": classify_error(str(obj.get("error") or obj.get("status"))) or "error", "detail": str(obj.get("error") or obj.get("status"))[:300]})
    return out


# ---------------------------------------------------------------- Copilot CLI (schema undocumented)

SESSION_KEYS = ("sessionId", "session_id", "sessionID")


def parse_copilot(obj: dict) -> list[dict]:
    out = []
    sid = next((obj[k] for k in SESSION_KEYS if isinstance(obj.get(k), str)), None)
    typ = str(obj.get("type") or obj.get("event") or "")
    if sid and typ in ("init", "session.start", "session_start", "session"):
        out.append({"kind": "init", "session": sid, "model": obj.get("model") if isinstance(obj.get("model"), str) else None})
    elif sid:
        out.append({"kind": "other", "session": sid})
    if re.search(r"tool", typ, re.I):
        out.append({"kind": "tool", "name": obj.get("toolName") or obj.get("tool") or obj.get("name")})
    u = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
    if u:
        out.append({"kind": "usage", "scope": "cumulative", "input": _int(u.get("input_tokens") or u.get("inputTokens")),
                    "output": _int(u.get("output_tokens") or u.get("outputTokens"))})
    err = obj.get("error")
    if err:
        text = err if isinstance(err, str) else json.dumps(err)
        out.append({"kind": classify_error(text) or "error", "detail": text[:300]})
    if typ in ("result", "done", "session.end", "complete"):
        out.append({"kind": "result", "ok": not err, "text": str(obj.get("result") or obj.get("text") or "")[:500], "session": sid})
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
    """Best-effort extraction of GenAI token totals from Copilot's OTel JSONL export
    (attributes gen_ai.usage.input_tokens / output_tokens per OTel GenAI semantic conventions).
    Returns None unless such attributes are actually found."""
    inp = out = 0
    found = False
    for ln in text.splitlines():
        for key, bucket in (("gen_ai.usage.input_tokens", "i"), ("gen_ai.usage.output_tokens", "o")):
            for m in re.finditer(r'"' + re.escape(key) + r'"\s*:\s*(?:\{[^{}]*?"(?:intValue|doubleValue)"\s*:\s*"?(\d+)"?[^{}]*\}|(\d+))', ln):
                v = int(m.group(1) or m.group(2))
                found = True
                if bucket == "i":
                    inp += v
                else:
                    out += v
    return {"input": inp, "output": out} if found else None
