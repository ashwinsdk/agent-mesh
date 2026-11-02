"""Pure data layer for every dashboard (TUI, --json, web, report). Read-only; never writes; never invents values.

Every metric is either measured (reported), derived locally (estimated/heuristic, labelled) or None (unavailable).
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict

from . import workers as W
from .db import Paths, now

VERSION = 1
LEAF_SKIP = ("project", "epic")


def _ro(paths: Paths):
    c = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    return c


def _rows(c, sql, a=()):
    return [dict(r) for r in c.execute(sql, a).fetchall()]


def _series(c):
    return _rows(c, "SELECT * FROM usage u WHERE worker_id IS NOT NULL AND id=(SELECT MAX(id) FROM usage WHERE worker_id=u.worker_id AND cli=u.cli)") + \
        _rows(c, "SELECT * FROM usage u WHERE worker_id IS NULL AND session_id IS NOT NULL AND id=(SELECT MAX(id) FROM usage WHERE session_id=u.session_id AND worker_id IS NULL)")


def _agg(rows, key):
    out = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "series": 0, "tokens_known": 0,
                               "api_spend": 0.0, "informational_api_equiv": 0.0, "unclassified_cost": 0.0, "cost_sources": set()})
    for r in rows:
        k = key(r) or "unknown"
        a = out[k]
        a["series"] += 1
        if r["input_tokens"] is not None or r["output_tokens"] is not None:
            a["tokens_known"] += 1
        a["input"] += r["input_tokens"] or 0
        a["output"] += r["output_tokens"] or 0
        a["cache_read"] += r["cache_read"] or 0
        a["cache_create"] += r["cache_create"] or 0
        if r["cost_usd"] is not None:
            mode = r.get("billing_mode") or "unknown"
            a["cost_sources"].add(r.get("cost_source") or r["source"])
            if mode == "api":
                a["api_spend"] += r["cost_usd"]
            elif mode == "subscription":
                a["informational_api_equiv"] += r["cost_usd"]
            else:
                a["unclassified_cost"] += r["cost_usd"]
    for a in out.values():
        a["total"] = a["input"] + a["output"]
        a["cost_sources"] = sorted(a["cost_sources"])
        a["tokens_known_fraction"] = f"{a['tokens_known']}/{a['series']}"
    return dict(out)


def _agent_state(s: dict) -> str:
    """State for non-worker sessions (orchestrator / interactive CLIs). Heuristic: derived from liveness only."""
    if s["status"] == "active":
        age = now() - s["last_seen"]
        return "WORKING" if age < 180 else "IDLE" if age < 3600 else "TERMINATED"
    return {"ended": "COMPLETED", "crashed": "FAILED", "failed": "FAILED"}.get(s["status"], "TERMINATED")


def build(paths: Paths) -> dict:
    c = _ro(paths)
    t_now = now()
    try:
        cfg_name = paths.root.name
        tasks = _rows(c, "SELECT * FROM tasks WHERE kind!='project'")
        deps = defaultdict(list)
        for d in _rows(c, "SELECT * FROM task_deps"):
            deps[d["task_id"]].append(d["dep_id"])
        by_id = {t["id"]: t for t in tasks}
        wrows = _rows(c, "SELECT * FROM workers ORDER BY started")
        sess = _rows(c, "SELECT * FROM sessions ORDER BY started")
        series = _series(c)
        u_by_sess = {}
        for r in series:
            u_by_sess.setdefault(r["worker_id"] or r["session_id"], []).append(r)
        # ---- agents
        agents = []
        wlast = {w["id"]: _rows(c, "SELECT type, ts FROM events WHERE actor=? AND type NOT IN ('tool','worker.state') ORDER BY id DESC LIMIT 1", (w["id"],)) for w in wrows}
        for w in wrows:
            t = by_id.get(w["task_id"])
            le = wlast[w["id"]]
            agents.append({"id": w["id"], "name": w["agent"], "role": w["role"], "cli": w["cli"], "model": w["model"], "session": w["native_session"] or w["id"],
                           "pid": w["pid"], "runner_pid": w["runner_pid"], "task": w["task_id"], "state": w["state"],
                           "started": w["started"], "duration": int((w["ended"] or t_now) - w["started"]),
                           "last_activity_age": int(t_now - (w["last_output"] or w["last_event"] or w["started"])),
                           "progress": (1.0 if t and t["status"] == "COMPLETED" else 0.8 if t and t["status"] == "REVIEW" else None),
                           "blocker": w["blocker"] or (w["state_detail"] if w["state"] in ("BLOCKED", "FAILED") else None),
                           "latest": (w["last_activity"] or (le[0]["type"] if le else None)), "restarts": w["restarts"], "kind": "worker"})
        wids = {w["id"] for w in wrows}
        for s in sess:
            if s["id"] in wids:
                continue
            agents.append({"id": s["id"], "name": s["agent"] or s["role"] or "interactive", "role": s["role"] or "interactive", "cli": s["cli"], "model": s["model"],
                           "session": s["native_id"] or s["id"], "pid": s["pid"], "runner_pid": None, "task": s["task_id"], "state": _agent_state(s),
                           "started": s["started"], "duration": int((s["ended"] or t_now) - s["started"]), "last_activity_age": int(t_now - s["last_seen"]),
                           "progress": None, "blocker": None, "latest": None, "restarts": 0, "kind": "session", "state_heuristic": True})
        # ---- sessions
        tot = {"created": len(sess), "active": sum(1 for s in sess if s["status"] == "active"),
               "completed": sum(1 for s in sess if s["status"] == "ended"), "failed": sum(1 for s in sess if s["status"] in ("failed", "crashed")),
               "terminated": sum(1 for s in sess if s["status"] in ("terminated", "rolled-over"))}
        sess_list = []
        for s in sess:
            u = u_by_sess.get(s["id"], [])
            w = next((x for x in wrows if x["id"] == s["id"]), None)
            sess_list.append({"id": s["id"], "cli": s["cli"], "model": s["model"], "agent": s["agent"], "status": s["status"], "task": s["task_id"],
                              "duration": int((s["ended"] or t_now) - s["started"]), "restarts": (w["restarts"] if w else 0),
                              "tokens": sum((x["input_tokens"] or 0) + (x["output_tokens"] or 0) for x in u) if u else None})
        # ---- tasks
        counts = defaultdict(int)
        for t in tasks:
            counts[t["status"]] += 1
        leaves = [t for t in tasks if t["kind"] not in LEAF_SKIP and t["status"] != "CANCELLED"
                  and not any(x["parent_id"] == t["id"] for x in tasks)]
        done = sum(1 for t in leaves if t["status"] == "COMPLETED")
        weights = {"COMPLETED": 1.0, "REVIEW": 0.8, "IN_PROGRESS": 0.4, "ASSIGNED": 0.1, "NEEDS_REWORK": 0.5}
        pct = (sum(weights.get(t["status"], 0) for t in leaves) / len(leaves)) if leaves else 0.0
        vlast = {r["task_id"]: r for r in _rows(c, "SELECT * FROM verifications v WHERE id=(SELECT MAX(id) FROM verifications WHERE task_id=v.task_id)")}
        task_list = []
        for t in tasks:
            v = vlast.get(t["id"])
            task_list.append({"id": t["id"], "title": t["title"], "kind": t["kind"], "parent": t["parent_id"], "status": t["status"], "owner": t["session_id"],
                              "agent": t["agent"], "deps": deps.get(t["id"], []), "risk": t["risk"], "files": t["files"],
                              "duration": int(((t["completed"] or t_now) - t["started"])) if t["started"] else None,
                              "pct": weights.get(t["status"], 0.0), "verified": (None if not v else bool(v["ok"])), "weak": bool(v["weak"]) if v else None,
                              "branch": t["branch"], "handoff": t["handoff"]})
        # ---- comms
        msgs = _rows(c, "SELECT * FROM messages ORDER BY ts DESC LIMIT 40")
        pend = _rows(c, "SELECT * FROM messages WHERE requires_ack=1 AND acked=0 AND delivery_state!='failed' ORDER BY ts")
        fails = _rows(c, "SELECT * FROM messages WHERE delivery_state='failed' ORDER BY ts DESC LIMIT 10")
        # ---- usage
        per_cli = _agg(series, lambda r: r["cli"])
        per_model = _agg(series, lambda r: r["model"])
        per_agent = _agg(series, lambda r: r["agent"])
        per_task = _agg([r for r in series if r["task_id"]], lambda r: r["task_id"])
        per_sess = _agg(series, lambda r: r["worker_id"] or r["session_id"])
        glob = _agg(series, lambda r: "all").get("all") or {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "total": 0, "series": 0,
                                                              "tokens_known": 0, "tokens_known_fraction": "0/0", "api_spend": 0.0,
                                                              "informational_api_equiv": 0.0, "unclassified_cost": 0.0, "cost_sources": []}
        completed = counts.get("COMPLETED", 0)
        spend = glob.get("api_spend", 0.0) if glob else 0.0
        quota = _rows(c, "SELECT * FROM usage WHERE five_hour_pct IS NOT NULL ORDER BY ts DESC LIMIT 1")
        rl = _rows(c, "SELECT * FROM ratelimits WHERE ts>? ORDER BY ts DESC LIMIT 10", (t_now - 86400,))
        hist = defaultdict(int)
        prev = {}
        for r in _rows(c, "SELECT ts, worker_id, session_id, cli, input_tokens i, output_tokens o FROM usage WHERE i IS NOT NULL OR o IS NOT NULL ORDER BY ts LIMIT 20000"):
            k = (r["worker_id"] or r["session_id"], r["cli"])
            cur = (r["i"] or 0) + (r["o"] or 0)
            d = max(0, cur - prev.get(k, 0))
            prev[k] = max(prev.get(k, 0), cur)
            hist[time.strftime("%Y-%m-%d", time.localtime(r["ts"]))] += d
        # ---- project
        active_workers = [w for w in wrows if w["state"] not in W.TERMINAL]
        ev = _rows(c, "SELECT * FROM events ORDER BY id DESC LIMIT 400")
        integ = [e for e in ev if e["type"].startswith("integrate.")]
        halted = c.execute("SELECT value FROM meta WHERE key='halted'").fetchone()
        phase = ("halted" if halted else "planning" if not leaves else "complete" if leaves and done == len(leaves) else
                 "executing" if active_workers or any(t["status"] in ("IN_PROGRESS", "ASSIGNED") for t in leaves) else
                 "review/integration" if any(t["status"] in ("REVIEW", "NEEDS_REWORK") for t in leaves) else "ready to dispatch")
        epics = [t for t in tasks if t["kind"] == "epic"]
        def epic_pct(e):
            ls = [t for t in leaves if _under(by_id, t, e["id"])]
            return (sum(weights.get(t["status"], 0) for t in ls) / len(ls)) if ls else 0.0
        workstreams = [{"id": e["id"], "title": e["title"], "pct": epic_pct(e)} for e in epics]
        blockers = [{"task": t["id"], "title": t["title"], "why": (t["notes"] or "").strip().splitlines()[-1][:100] if t["notes"] else t["status"]}
                    for t in tasks if t["status"] in ("BLOCKED", "FAILED", "NEEDS_REWORK")]
        blockers += [{"worker": w["id"], "why": w["state_detail"]} for w in wrows if w["state"] in ("BLOCKED", "FAILED")]
        recent = [{"ts": e["ts"], "type": e["type"], "actor": e["actor"], "task": e["task_id"]} for e in ev
                  if e["type"] in ("task.approved", "task.rejected", "task.submitted", "integrate.ok", "integrate.conflict", "integrate.revert", "worker.spawn",
                                   "worker.failed", "worker.limit", "worker.fallback", "halt")][:12]
        alerts = []
        for r in rl[:3]:
            alerts.append({"level": "bad", "text": f"rate/usage limit on {r['cli']} ({r['session_id']}) {int(t_now - r['ts'])}s ago"})
        for w in active_workers:
            if t_now - (w["last_output"] or w["started"]) > 600:
                alerts.append({"level": "warn", "text": f"{w['id']} silent for {int(t_now - (w['last_output'] or w['started']))}s"})
        if pend:
            alerts.append({"level": "warn", "text": f"{len(pend)} message(s) awaiting acknowledgement"})
        if fails:
            alerts.append({"level": "bad", "text": f"{len(fails)} message delivery failure(s)"})
        if halted:
            alerts.append({"level": "bad", "text": "HALTED: new spawns blocked (mesh unhalt)"})
        return {
            "version": VERSION, "generated": t_now,
            "project": {"name": cfg_name, "phase": phase, "progress": pct, "leaf_tasks": len(leaves), "completed_leaf": done, "workstreams": workstreams,
                        "milestones": {"completed": [w["title"] for w in workstreams if w["pct"] >= 1.0], "pending": [w["title"] for w in workstreams if w["pct"] < 1.0]},
                        "blockers": blockers, "recent_changes": recent,
                        "validation": {"verified": sum(1 for v in vlast.values() if v["ok"]), "failed": sum(1 for v in vlast.values() if not v["ok"]),
                                       "weak": sum(1 for v in vlast.values() if v["weak"])},
                        "integration": {"ok": sum(1 for e in integ if e["type"] == "integrate.ok"),
                                        "conflicts": sum(1 for e in integ if e["type"] == "integrate.conflict"),
                                        "reverts": sum(1 for e in integ if e["type"] == "integrate.revert")}},
            "agents": agents,
            "sessions": {"totals": tot, "per_cli": _count(sess, "cli"), "per_model": _count(sess, "model"), "list": sess_list,
                         "restarts_total": sum(w["restarts"] for w in wrows)},
            "tasks": {"counts": dict(counts), "total": len(tasks), "list": task_list},
            "comms": {"recent": [{"id": m["id"], "ts": m["ts"], "from": m["sender"], "to": m["recipient"], "type": m["type"], "task": m["task_id"],
                                  "prio": m["priority"], "state": m["delivery_state"], "acked": bool(m["acked"]), "attempts": m["attempts"],
                                  "text": m["content"][:100]} for m in msgs],
                      "pending_acks": len(pend), "failures": [{"id": m["id"], "to": m["recipient"], "attempts": m["attempts"]} for m in fails]},
            "usage": {"global": glob, "per_cli": per_cli, "per_model": per_model, "per_agent": per_agent, "per_task": per_task, "per_session": per_sess,
                      "avg_api_cost_per_completed_task": (spend / completed) if (completed and spend) else None,
                      "quota": ({"five_hour_pct": quota[0]["five_hour_pct"], "five_hour_reset": quota[0]["five_hour_reset"],
                                 "seven_day_pct": quota[0]["seven_day_pct"], "source": "reported (Claude statusline, subscription accounts)"} if quota else None),
                      "rate_limit_events": rl, "history_tokens_by_day": dict(sorted(hist.items())),
                      "notes": ["cost: 'api_spend' counts only billing_mode=api; subscription usage is shown as informational API-equivalent, never as spend",
                                "Antigravity/Copilot expose no quota; Copilot tokens come from its OTel export when present",
                                "None/n-a means the CLI does not expose the metric"]},
            "alerts": alerts, "halted": bool(halted),
        }
    finally:
        c.close()


def _under(by_id, t, ancestor) -> bool:
    cur = t
    while cur and cur["parent_id"]:
        if cur["parent_id"] == ancestor:
            return True
        cur = by_id.get(cur["parent_id"])
    return False


def _count(rows, key):
    out = defaultdict(int)
    for r in rows:
        out[r[key] or "unknown"] += 1
    return dict(out)


def _n(v):
    return "n/a" if v is None else (f"{v / 1e6:.2f}M" if v >= 1e6 else f"{v / 1e3:.1f}k" if v >= 1e3 else str(int(v)))


def render_report(s: dict) -> str:
    p, t, u = s["project"], s["tasks"], s["usage"]
    g = u["global"] or {}
    lines = [f"# Project report: {p['name']}", f"phase: {p['phase']}   progress: {p['progress'] * 100:.0f}% ({p['completed_leaf']}/{p['leaf_tasks']} tasks completed)",
             f"tasks: " + ", ".join(f"{k}={v}" for k, v in sorted(t['counts'].items())),
             f"validation: {p['validation']['verified']} verified, {p['validation']['failed']} failed, {p['validation']['weak']} weak | "
             f"integration: {p['integration']['ok']} merged, {p['integration']['conflicts']} conflicts",
             f"sessions: {s['sessions']['totals']}  restarts={s['sessions']['restarts_total']}", "",
             "## Tokens (only what CLIs report)",
             f"total in/out: {_n(g.get('input'))}/{_n(g.get('output'))}  cache read: {_n(g.get('cache_read'))}   series with token data: {g.get('tokens_known_fraction', 'n/a')}"]
    for k, a in sorted(u["per_cli"].items()):
        lines.append(f"  {k:12} in/out {_n(a['input'])}/{_n(a['output'])}  api spend ${a['api_spend']:.4f}  informational api-equiv ${a['informational_api_equiv']:.4f}  tokens from {a['tokens_known_fraction']} series")
    lines.append(f"API spend (billing_mode=api only): ${g.get('api_spend', 0):.4f}   avg per completed task: "
                 + (f"${u['avg_api_cost_per_completed_task']:.4f}" if u["avg_api_cost_per_completed_task"] else "n/a"))
    lines += ["", "## Tasks"] + [f"  {x['id']} [{x['status']}] {x['title']}" + (f" verified={x['verified']}" if x["verified"] is not None else "") for x in t["list"] if x["kind"] not in LEAF_SKIP]
    if p["blockers"]:
        lines += ["", "## Blockers"] + [f"  {b}" for b in p["blockers"]]
    return "\n".join(lines)
