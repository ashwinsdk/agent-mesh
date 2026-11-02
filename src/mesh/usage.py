"""Usage records with honest provenance: every row says reported | estimated | unavailable."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .db import DB, now

SOURCES = ("reported", "estimated", "unavailable")


def record(db: DB, session: str | None, cli: str, source: str, task_id: str | None = None, agent: str | None = None,
           model: str | None = None, worker_id: str | None = None, billing_mode: str | None = None,
           cost_source: str | None = None, turn: int | None = None, **m) -> None:
    assert source in SOURCES
    keys = ["input_tokens", "output_tokens", "cache_read", "cache_create", "context_tokens", "context_size",
            "context_pct", "cost_usd", "five_hour_pct", "five_hour_reset", "seven_day_pct", "seven_day_reset",
            "tool_calls", "requests", "worker_id", "billing_mode", "cost_source", "turn", "ai_credits"]
    m = {**m, "worker_id": worker_id, "billing_mode": billing_mode, "cost_source": cost_source, "turn": turn}
    vals = [m.get(k) for k in keys]
    with db.tx() as c:
        c.execute(f"INSERT INTO usage(ts,session_id,task_id,agent,cli,model,source,{','.join(keys)}) VALUES(?,?,?,?,?,?,?,{','.join('?' * len(keys))})",
                  (now(), session, task_id, agent, cli, model, source, *vals))


def from_claude_statusline(payload: dict) -> dict:
    """Map Claude Code's statusline JSON (documented fields only) to usage metrics. Missing => None."""
    cw = payload.get("context_window") or {}
    cu = cw.get("current_usage") or {}
    rl = payload.get("rate_limits") or {}
    fh, sd = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    cost = payload.get("cost") or {}
    return dict(
        input_tokens=cw.get("total_input_tokens"), output_tokens=cw.get("total_output_tokens"),
        cache_read=cu.get("cache_read_input_tokens"), cache_create=cu.get("cache_creation_input_tokens"),
        context_size=cw.get("context_window_size"), context_pct=cw.get("used_percentage"),
        cost_usd=cost.get("total_cost_usd"),
        five_hour_pct=fh.get("used_percentage"), five_hour_reset=fh.get("resets_at"),
        seven_day_pct=sd.get("used_percentage"), seven_day_reset=sd.get("resets_at"),
    )


def estimate_from_file(path: str | None, context_size: int = 200_000) -> dict | None:
    """Fallback for CLIs that expose no token counts: ~4 bytes/token over the transcript. Labelled estimated."""
    if not path:
        return None
    try:
        size = Path(path).stat().st_size
    except OSError:
        return None
    toks = size // 4
    return dict(context_tokens=toks, context_size=context_size, context_pct=round(min(100.0, 100 * toks / context_size), 1))


def latest_series(db: DB):
    """Latest cumulative usage row per (worker, cli): the correct unit for aggregation (a worker can span CLIs after fallback)."""
    return db.q("SELECT * FROM usage u WHERE worker_id IS NOT NULL AND id=(SELECT MAX(id) FROM usage WHERE worker_id=u.worker_id AND cli=u.cli)")


def latest(db: DB, session: str):
    return db.one("SELECT * FROM usage WHERE session_id=? ORDER BY ts DESC LIMIT 1", (session,))


def record_rate_limit(db: DB, session: str | None, cli: str, detail: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO ratelimits(ts,session_id,cli,detail) VALUES(?,?,?,?)", (now(), session, cli, detail[:500]))
        db.event(session, "ratelimit", None, {"cli": cli, "detail": detail[:200]}, conn=c)


def report(db: DB) -> dict:
    """Machine-readable report; never invents values: None means unavailable."""
    sessions = []
    for s in db.q("SELECT * FROM sessions ORDER BY started"):
        u = latest(db, s["id"])
        n_tools = db.one("SELECT COUNT(*) FROM events WHERE actor=? AND type='tool'", (s["id"],))[0]
        sessions.append({
            "session": s["id"], "cli": s["cli"], "agent": s["agent"], "model": s["model"], "status": s["status"],
            "duration_s": round((s["ended"] or now()) - s["started"]), "task": s["task_id"],
            "tool_events": n_tools,
            "usage": ({k: u[k] for k in u.keys() if k not in ("id",)} if u else {"source": "unavailable"}),
        })
    per_task = [dict(r) for r in db.q(
        "SELECT task_id, MAX(input_tokens) in_tok, MAX(output_tokens) out_tok, source FROM usage WHERE task_id IS NOT NULL GROUP BY task_id, session_id")]
    return {"generated": now(), "sessions": sessions, "per_task": per_task,
            "rate_limit_events": [dict(r) for r in db.q("SELECT * FROM ratelimits ORDER BY ts DESC LIMIT 20")],
            "note": "source=reported is provider-measured; estimated is derived locally; unavailable/None means the CLI does not expose it."}
