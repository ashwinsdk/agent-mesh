"""Terminal dashboard. Read-only SQLite, 1s refresh, plain-text fallback (--once / non-tty)."""
from __future__ import annotations

import json
import sqlite3
import time

from . import tasks
from .db import Paths, now


def _ro(paths: Paths):
    c = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    return c


def _bar(pct, w=12):
    if pct is None:
        return "[" + "?" * w + "]  n/a"
    n = int(round(w * min(100, pct) / 100))
    return "[" + "#" * n + "." * (w - n) + f"] {pct:3.0f}%"


def _ago(ts):
    if not ts:
        return "-"
    d = int(now() - ts)
    return f"{d}s" if d < 90 else f"{d // 60}m" if d < 5400 else f"{d // 3600}h"


def _num(v):
    return "n/a" if v is None else (f"{v / 1000:.1f}k" if v >= 1000 else str(int(v)))


def snapshot(paths: Paths, width: int = 160) -> list[tuple[str, str]]:
    """Return [(text, style)] with style in head|ok|warn|bad|dim|"". """
    c = _ro(paths)
    L: list[tuple[str, str]] = []
    add = lambda t="", s="": L.append((t[:width], s))
    st = {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM tasks WHERE kind!='project' GROUP BY status")}
    add(f" agent-mesh  {time.strftime('%H:%M:%S')}   tasks: " + ("  ".join(f"{k}={v}" for k, v in sorted(st.items())) or "none"), "head")
    add()
    add(" SESSIONS  (usage source: R=reported E=estimated -=unavailable)", "head")
    add(f" {'session':<24}{'cli':<13}{'agent':<14}{'task':<12}{'seen':>5}  {'context':<20}{'tokens in/out':>16}  src  model", "dim")
    for s in c.execute("SELECT * FROM sessions ORDER BY status, last_seen DESC LIMIT 12"):
        u = c.execute("SELECT * FROM usage WHERE session_id=? ORDER BY ts DESC LIMIT 1", (s["id"],)).fetchone()
        src = {"reported": "R", "estimated": "E"}.get(u["source"], "-") if u else "-"
        pct = u["context_pct"] if u else None
        style = "ok" if s["status"] == "active" and now() - s["last_seen"] < 600 else "dim"
        add(f" {s['id']:<24}{s['cli']:<13}{(s['agent'] or '-'):<14}{(s['task_id'] or '-'):<12}{_ago(s['last_seen']):>5}  {_bar(pct):<20}"
            f"{(_num(u['input_tokens']) + '/' + _num(u['output_tokens'])) if u else 'n/a':>16}  {src:<4} {s['model'] or '-'}", style)
        if u and (u["five_hour_pct"] is not None):
            add(f"   quota: 5h {u['five_hour_pct']:.0f}% (resets {time.strftime('%H:%M', time.localtime(u['five_hour_reset'])) if u['five_hour_reset'] else '?'})"
                f"  7d {('%.0f%%' % u['seven_day_pct']) if u['seven_day_pct'] is not None else 'n/a'}  [reported]", "dim")
        elif s["cli"] != "claude" or not u:
            add("   quota: unavailable from this CLI", "dim")
    add()
    add(" TASKS", "head")
    for r in c.execute("SELECT * FROM tasks WHERE kind!='project' AND status NOT IN ('COMPLETED','CANCELLED','BACKLOG') ORDER BY priority, created LIMIT 14"):
        style = "bad" if r["status"] in ("FAILED", "NEEDS_REWORK") else "warn" if r["status"] in ("BLOCKED", "PAUSED") else "ok" if r["status"] == "IN_PROGRESS" else ""
        add(f" {r['id']:<11}{r['status']:<13}{(r['session_id'] or '-'):<18}{r['title'][:40]:<42}active {_ago(r['last_activity'])}", style)
    done = c.execute("SELECT COUNT(*) FROM tasks WHERE status='COMPLETED'").fetchone()[0]
    add(f" completed: {done}", "dim")
    add()
    add(" LOCKS / MESSAGES / CHECKPOINTS / ALERTS", "head")
    for r in c.execute("SELECT * FROM locks ORDER BY acquired DESC LIMIT 5"):
        add(f" lock {r['pattern']:<30} {r['owner']:<18} expires in {int(r['expires'] - now())}s", "dim")
    unread = c.execute("SELECT COUNT(*) FROM messages WHERE acked=0").fetchone()[0]
    cp = c.execute("SELECT created, task_id FROM checkpoints ORDER BY created DESC LIMIT 1").fetchone()
    add(f" unacked messages: {unread}   last checkpoint: {(_ago(cp['created']) + ' ago (' + str(cp['task_id']) + ')') if cp else 'none'}", "")
    for r in c.execute("SELECT * FROM ratelimits WHERE ts > ? ORDER BY ts DESC LIMIT 3", (now() - 3600,)):
        add(f" RATE LIMIT {r['cli']} {r['session_id']} {_ago(r['ts'])} ago", "bad")
    for r in tasks.abandoned(_DBShim(c), 1800)[:3]:
        add(f" ABANDONED? {r['id']} owner {r['session_id']} silent {_ago(r['last_activity'])}", "warn")
    for r in c.execute("SELECT * FROM events WHERE type IN ('safety.deny','lock.denied','task.claim_conflict','integrate.conflict','checkpoint.corrupt') ORDER BY ts DESC LIMIT 4"):
        add(f" {r['type']:<22} {r['actor'] or '-'} {_ago(r['ts'])} ago {(r['data'] or '')[:60]}", "warn")
    c.close()
    return L


class _DBShim:
    """Lets tasks.abandoned() run on a read-only connection."""
    def __init__(self, conn):
        self.conn = conn

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()


def live(paths: Paths, interval: float = 1.0) -> None:
    import curses

    def main(scr):
        curses.curs_set(0)
        curses.use_default_colors()
        for i, col in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED), 1):
            curses.init_pair(i, col, -1)
        scr.timeout(int(interval * 1000))
        sty = {"head": curses.color_pair(1) | curses.A_BOLD, "ok": curses.color_pair(2), "warn": curses.color_pair(3),
               "bad": curses.color_pair(4) | curses.A_BOLD, "dim": curses.A_DIM, "": 0}
        while True:
            h, w = scr.getmaxyx()
            try:
                lines = snapshot(paths, w - 1)
            except sqlite3.Error as e:
                lines = [(f"db busy/unavailable: {e}", "bad")]
            scr.erase()
            for i, (t, s) in enumerate(lines[:h - 1]):
                scr.addnstr(i, 0, t, w - 1, sty.get(s, 0))
            scr.addnstr(h - 1, 0, " q quit ", w - 1, curses.A_REVERSE)
            scr.refresh()
            if scr.getch() in (ord("q"), 27):
                return

    curses.wrapper(main)
