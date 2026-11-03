"""Human-readable activity feed for the app: turns raw events/commands/messages into a short, plain-language timeline.
Stdlib only (no UI dependency) so it is unit-testable and reusable by `mesh status`."""
from __future__ import annotations

import json
import sqlite3

from .db import Paths


def _ro(paths: Paths):
    c = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    return c


def _title(c, tid):
    if not tid:
        return "a task"
    r = c.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
    return f"“{r['title']}”" if r else tid


def _worker_label(c, wid):
    r = c.execute("SELECT w.agent, w.cli, t.title FROM workers w LEFT JOIN tasks t ON t.id=w.task_id WHERE w.id=?", (wid,)).fetchone()
    if not r:
        return wid
    return f"{(r['title'] or r['agent'] or wid)[:40]} ({r['cli']})"


def activity(paths: Paths, limit: int = 80) -> list[dict]:
    """Newest `limit` items, oldest first. Each: {key, ts, kind: you|orchestrator|event|attention, level: info|ok|warn|bad, icon, text}."""
    c = _ro(paths)
    out: list[dict] = []
    try:
        for r in c.execute("SELECT * FROM commands WHERE kind='submit_prompt' ORDER BY id DESC LIMIT 20"):
            p = json.loads(r["payload"] or "{}")
            out.append({"key": f"c{r['id']}", "ts": r["ts"], "kind": "you", "level": "info", "icon": "you ▸", "text": p.get("prompt", "")})
        for r in c.execute("SELECT * FROM messages WHERE sender='orchestrator' AND type IN ('answer','decision_result') ORDER BY ts DESC LIMIT 20"):
            out.append({"key": f"m{r['id']}", "ts": r["ts"], "kind": "orchestrator", "level": "info", "icon": "orchestrator ▸", "text": r["content"]})
        for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 400"):
            d = json.loads(r["data"] or "{}")
            t, tid = r["type"], r["task_id"]
            lvl, icon, text = "info", "·", None
            if t == "run.status":
                lvl, icon = ("ok" if d["to"] in ("executing", "complete") else "warn" if d["to"] in ("paused", "halted", "failed") else "info"), "▣"
                text = {"planning": "Planning…", "awaiting_approval": "Plan is ready for your review", "executing": "Executing the plan",
                        "paused": "Paused: nothing new will be dispatched", "integrating": "All tasks done: running final validation",
                        "complete": "Run complete", "failed": "Run failed", "halted": "Halted: all workers stopped"}.get(d["to"], f"Run → {d['to']}")
            elif t == "plan.started":
                text = f"Asking {d.get('cli')} to plan the work…"
            elif t == "plan.failed":
                lvl, icon, text = "bad", "✗", f"Planning failed: {d.get('why', '')[:140]}"
            elif t == "worker.spawn":
                lvl, icon, text = "info", "▶", f"Started {d.get('agent')} on {_title(c, tid)} ({d.get('cli')})"
            elif t == "task.submitted":
                lvl, icon, text = "info", "◐", f"{_title(c, tid)} submitted for review"
            elif t == "verify.result":
                lvl, icon = ("ok", "✓") if d.get("ok") else ("warn", "✗")
                text = f"{_title(c, tid)} verification {'passed' if d.get('ok') else 'FAILED'}" + (" (weak: no checks ran)" if d.get("weak") else "")
            elif t == "task.approved":
                lvl, icon, text = "ok", "✓", f"{_title(c, tid)} approved"
            elif t == "task.rejected":
                lvl, icon, text = "warn", "↺", f"{_title(c, tid)} sent back for rework: {d.get('reason', '')[:100]}"
            elif t == "integrate.ok":
                lvl, icon, text = "ok", "⇢", f"{_title(c, tid)} merged into the integration branch"
            elif t in ("integrate.conflict", "integrate.revert"):
                lvl, icon, text = "bad", "⚠", f"{_title(c, tid)} could not be merged ({'conflict' if t.endswith('conflict') else 'tests failed after merge'})"
            elif t == "worker.limit":
                lvl, icon, text = "warn", "⚠", f"{d.get('cli')} hit its usage limit"
            elif t == "worker.fallback":
                lvl, icon, text = "warn", "→", f"Fell back from {d.get('from')} to {d.get('to')}"
            elif t == "worker.restart":
                lvl, icon, text = "warn", "↻", f"A worker was restarted ({d.get('outcome')}) on {_title(c, tid)}"
            elif t == "worker.failed":
                lvl, icon, text = "bad", "✗", f"A worker failed on {_title(c, tid)}: {d.get('why', '')[:100]}"
            elif t == "worker.exited_unsubmitted":
                lvl, icon, text = "warn", "⚠", f"A worker finished {_title(c, tid)} without submitting it"
            elif t == "worker.stalled":
                lvl, icon, text = "warn", "⏳", f"A worker has been silent for {d.get('idle_seconds', '?')}s"
            elif t == "dispatch.error":
                lvl, icon, text = "bad", "✗", f"Could not start {_title(c, tid)}: {d.get('error', '')[:120]}"
            elif t == "brain.turn":
                text = "Orchestrator is thinking about a question…"
            elif t == "brain.done":
                lvl, icon, text = "ok", "◆", f"Orchestrator answered ({d.get('actions', 0)} action(s))"
            elif t == "halt":
                lvl, icon, text = "bad", "■", "Halt requested"
            elif t == "attention.open":
                lvl, icon, text = "warn", "!", f"Needs you: {d.get('title', '')[:140]}"
            elif t == "config.changed_externally":
                lvl, icon, text = "warn", "⚠", ".mesh/mesh.toml was edited outside mesh: " + "; ".join(d.get("changes", [])[:3]) + " (last mesh-written copy: " + str(d.get("backup", "")).split("/.mesh/")[-1] + ")"
            elif t == "command.error":
                lvl, icon, text = "bad", "✗", f"Command {d.get('kind')} failed: {d.get('error', '')[:120]}"
            if text:
                out.append({"key": f"e{r['id']}", "ts": r["ts"], "kind": "attention" if t == "attention.open" else "event", "level": lvl, "icon": icon, "text": text})
    finally:
        c.close()
    out.sort(key=lambda x: (x["ts"], x["key"]))
    return out[-limit:]


_ECHO_KINDS = {"kill_session", "clear_session", "refresh_limits", "set_cli", "retry_task", "say", "promote", "approve_item", "reject_item", "halt", "resume", "pause"}


def command_results(paths: Paths, after_id: int) -> list[dict]:
    """Outcome of commands the user issued (so a refused kill or a failed promote is visible). `text` is None for ones not worth echoing;
    the caller still advances its cursor past them."""
    c = _ro(paths)
    try:
        rows = c.execute("SELECT id, kind, status, result FROM commands WHERE id>? AND status!='pending' ORDER BY id", (after_id,)).fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        show = r["status"] == "error" or r["kind"] in _ECHO_KINDS
        out.append({"id": r["id"], "kind": r["kind"], "status": r["status"], "text": (r["result"] or f"{r['kind']} failed") if show else None})
    return out
