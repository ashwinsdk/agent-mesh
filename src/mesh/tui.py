"""Terminal dashboard. Read-only SQLite, 1s refresh, plain-text fallback (--once / non-tty)."""
from __future__ import annotations

import json
import sqlite3
import time

from . import snapshot as snap_mod
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


# ====================================================================== v2: tabbed views over snapshot.build()
TABS = ["Overview", "Agents", "Sessions", "Tasks", "Comms", "Usage"]


def _dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    return f"{sec}s" if sec < 90 else f"{sec // 60}m" if sec < 5400 else f"{sec / 3600:.1f}h"


def _pbar(frac, w=14):
    if frac is None:
        return "-"
    n = int(round(w * max(0.0, min(1.0, frac))))
    return "[" + "#" * n + "." * (w - n) + f"] {frac * 100:3.0f}%"


STATE_STYLE = {"WORKING": "ok", "COMMUNICATING": "ok", "REVIEWING": "ok", "PLANNING": "ok", "INITIALISING": "ok", "IDLE": "dim",
               "BLOCKED": "warn", "PAUSED": "warn", "FAILED": "bad", "TERMINATED": "dim", "COMPLETED": "dim"}


def view_overview(s, w):
    p = s["project"]
    L = [(f" {p['name']}  phase: {p['phase']}   {_pbar(p['progress'], 24)}   {p['completed_leaf']}/{p['leaf_tasks']} tasks", "head"), ("", "")]
    c = s["tasks"]["counts"]
    L.append((" tasks  " + "  ".join(f"{k}={c[k]}" for k in ("BACKLOG", "READY", "ASSIGNED", "IN_PROGRESS", "REVIEW", "NEEDS_REWORK", "BLOCKED", "FAILED", "COMPLETED") if k in c) or " no tasks", ""))
    st = {}
    for a in s["agents"]:
        st[a["state"]] = st.get(a["state"], 0) + 1
    L.append((" agents " + ("  ".join(f"{k}={v}" for k, v in sorted(st.items())) or "none"), ""))
    sv = s["sessions"]["totals"]
    L.append((f" sessions created={sv['created']} active={sv['active']} completed={sv['completed']} failed={sv['failed']} terminated={sv['terminated']} restarts={s['sessions']['restarts_total']}", ""))
    g = s["usage"]["global"]
    L.append((f" tokens in/out {snap_mod._n(g['input'])}/{snap_mod._n(g['output'])} (from {g['tokens_known_fraction']} series)   API spend ${g['api_spend']:.4f}"
              f"   informational api-equiv ${g['informational_api_equiv']:.4f}", ""))
    L.append((f" validation: {p['validation']['verified']} verified / {p['validation']['failed']} failed / {p['validation']['weak']} weak    "
              f"integration: {p['integration']['ok']} merged / {p['integration']['conflicts']} conflicts", ""))
    L.append(("", ""))
    for al in s["alerts"]:
        L.append((f" ! {al['text']}", al["level"]))
    if p["workstreams"]:
        L += [("", ""), (" WORKSTREAMS", "head")] + [(f"  {x['id']:<12}{x['title'][:40]:<42}{_pbar(x['pct'])}", "") for x in p["workstreams"]]
        L.append((f"  milestones done: {len(p['milestones']['completed'])}  pending: {len(p['milestones']['pending'])}", "dim"))
    if p["blockers"]:
        L += [("", ""), (" BLOCKERS", "head")] + [(f"  {b.get('task') or b.get('worker')}: {b['why']}", "warn") for b in p["blockers"][:8]]
    if p["recent_changes"]:
        L += [("", ""), (" RECENT", "head")] + [(f"  {_dur(now() - r['ts']):>5} ago  {r['type']:<20}{r['actor'] or '-':<14}{r['task'] or ''}", "dim") for r in p["recent_changes"]]
    return L


def view_agents(s, w):
    L = [(" AGENTS (state for interactive sessions is a liveness heuristic)", "head"),
         (f" {'id':<14}{'name/role':<16}{'cli/model':<20}{'session':<10}{'pid':>7} {'task':<11}{'state':<14}{'dur':>6}{'last':>6}  {'progress':<20}{'blocker / latest'}", "dim")]
    for a in s["agents"]:
        L.append((f" {a['id']:<14}{(a['name'] or '-')[:15]:<16}{(a['cli'] + '/' + (a['model'] or '-'))[:19]:<20}{str(a['session'])[:9]:<10}{str(a['pid'] or '-'):>7} "
                  f"{(a['task'] or '-'):<11}{a['state']:<14}{_dur(a['duration']):>6}{_dur(a['last_activity_age']):>6}  {_pbar(a['progress'], 8) if a['progress'] is not None else '-':<20}"
                  f"{(a['blocker'] or a['latest'] or '')[:40]}", STATE_STYLE.get(a["state"], "")))
    return L


def view_sessions(s, w):
    sv = s["sessions"]
    L = [(" SESSIONS", "head"), (f" total created {sv['totals']['created']}  active {sv['totals']['active']}  completed {sv['totals']['completed']}  "
                                 f"failed {sv['totals']['failed']}  terminated {sv['totals']['terminated']}  restarts {sv['restarts_total']}", ""),
         (" per CLI:   " + "  ".join(f"{k}={v}" for k, v in sv["per_cli"].items()), ""),
         (" per model: " + "  ".join(f"{k}={v}" for k, v in sv["per_model"].items()), ""), ("", ""),
         (f" {'session':<16}{'cli':<12}{'model':<16}{'agent':<14}{'status':<12}{'task':<12}{'dur':>6}{'restarts':>9}{'tokens':>10}", "dim")]
    for x in reversed(sv["list"][-30:]):
        L.append((f" {x['id']:<16}{x['cli']:<12}{(x['model'] or '-')[:15]:<16}{(x['agent'] or '-')[:13]:<14}{x['status']:<12}{(x['task'] or '-'):<12}{_dur(x['duration']):>6}{x['restarts']:>9}{snap_mod._n(x['tokens']):>10}",
                  "ok" if x["status"] == "active" else "dim"))
    return L


def view_tasks(s, w):
    t = s["tasks"]
    L = [(f" TASKS total={t['total']}  " + "  ".join(f"{k}={v}" for k, v in sorted(t["counts"].items())), "head"), ("", "")]
    by_parent = {}
    for x in t["list"]:
        by_parent.setdefault(x["parent"], []).append(x)
    def walk(pid, depth):
        for x in by_parent.get(pid, []):
            style = "bad" if x["status"] in ("FAILED", "NEEDS_REWORK") else "warn" if x["status"] in ("BLOCKED", "PAUSED") else "ok" if x["status"] in ("IN_PROGRESS", "REVIEW") else "dim" if x["status"] == "COMPLETED" else ""
            ver = "" if x["verified"] is None else (" ✓verified" if x["verified"] else " ✗verify-failed")
            dep = (" ← " + ",".join(x["deps"])) if x["deps"] else ""
            L.append(("  " + "  " * depth + f"{x['id']} [{x['status']}] {x['title'][:38]} owner={x['owner'] or '-'} {_dur(x['duration'])} risk={x['risk']}{ver}{dep}", style))
            walk(x["id"], depth + 1)
    walk(None, 0)
    return L


def view_comms(s, w):
    c = s["comms"]
    L = [(f" COMMUNICATION  pending acks: {c['pending_acks']}   delivery failures: {len(c['failures'])}", "head"),
         (f" {'age':>5} {'from':<16}{'to':<18}{'type':<16}{'task':<11}{'prio':>4} {'state':<10}{'tries':>5}  text", "dim")]
    for m in c["recent"]:
        L.append((f" {_dur(now() - m['ts']):>5} {m['from'][:15]:<16}{m['to'][:17]:<18}{m['type']:<16}{(m['task'] or '-'):<11}{m['prio']:>4} {m['state']:<10}{m['attempts']:>5}  {m['text']}",
                  "bad" if m["state"] == "failed" else "warn" if (not m["acked"] and m["state"] != "pending") else ""))
    return L


def view_usage(s, w):
    u = s["usage"]
    L = [(" USAGE  (reported = measured by the CLI; n/a = not exposed)", "head")]
    def table(title, d):
        L.append(("", ""))
        L.append((f" {title}", "head"))
        L.append((f" {'':<22}{'input':>9}{'output':>9}{'cache rd':>10}{'tokens-from':>12}{'API spend':>11}{'info api-eq':>12}{'AIU':>9}", "dim"))
        for k, a in sorted(d.items(), key=lambda kv: -kv[1]["total"])[:12]:
            L.append((f" {str(k)[:21]:<22}{snap_mod._n(a['input']):>9}{snap_mod._n(a['output']):>9}{snap_mod._n(a['cache_read']):>10}{a['tokens_known_fraction']:>12}"
                      f"{'$%.4f' % a['api_spend']:>11}{'$%.4f' % a['informational_api_equiv']:>12}{('%.3f' % a['ai_credits']) if a['ai_credits'] is not None else 'n/a':>9}", ""))
    table("GLOBAL", {"all": u["global"]})
    for t, d in (("PER CLI", u["per_cli"]), ("PER MODEL", u["per_model"]), ("PER AGENT", u["per_agent"]), ("PER TASK", u["per_task"]), ("PER SESSION", u["per_session"])):
        table(t, d)
    L.append(("", ""))
    L.append((" AIU = GitHub Copilot's metering unit as reported by its CLI (nano-AIU / 1e9); it is not converted to currency", "dim"))
    L.append((f" avg API cost per completed task: " + (f"${u['avg_api_cost_per_completed_task']:.4f}" if u["avg_api_cost_per_completed_task"] else "n/a (no api-mode spend)"), ""))
    q = u["quota"]
    L.append((" quota: " + (f"5h {q['five_hour_pct']:.0f}% used, resets {time.strftime('%H:%M', time.localtime(q['five_hour_reset'])) if q['five_hour_reset'] else '?'}; "
                            f"7d {('%.0f%%' % q['seven_day_pct']) if q['seven_day_pct'] is not None else 'n/a'}  [{q['source']}]" if q else "unavailable (no CLI is reporting a usage window)"), ""))
    if u["rate_limit_events"]:
        L += [(f" RATE LIMIT {r['cli']} {r['session_id']} {_dur(now() - r['ts'])} ago", "bad") for r in u["rate_limit_events"][:5]]
    if u["history_tokens_by_day"]:
        L.append((" history (tokens/day): " + "  ".join(f"{d[5:]}:{snap_mod._n(v)}" for d, v in list(u["history_tokens_by_day"].items())[-7:]), "dim"))
    L += [(f" note: {n}", "dim") for n in u["notes"]]
    return L


VIEWS = {"Overview": view_overview, "Agents": view_agents, "Sessions": view_sessions, "Tasks": view_tasks, "Comms": view_comms, "Usage": view_usage}


def render(snap, tab: str, width: int = 160):
    return [(t[:width], st) for t, st in VIEWS[tab](snap, width)]


def render_all(paths: Paths, width: int = 160) -> str:
    snap = snap_mod.build(paths)
    out = []
    for tab in TABS:
        out.append(f"==== {tab} ====")
        out += [t for t, _ in render(snap, tab, width)]
        if tab == "Sessions":
            out += [t for t, _ in snapshot(paths, width)[2:]]  # classic per-session context/quota rows
        out.append("")
    return "\n".join(out)


def live(paths: Paths, interval: float = 1.0, start_tab: int = 0) -> None:
    import curses

    def main(scr):
        curses.curs_set(0)
        curses.use_default_colors()
        for i, col in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED), 1):
            curses.init_pair(i, col, -1)
        scr.timeout(int(interval * 1000))
        sty = {"head": curses.color_pair(1) | curses.A_BOLD, "ok": curses.color_pair(2), "warn": curses.color_pair(3),
               "bad": curses.color_pair(4) | curses.A_BOLD, "dim": curses.A_DIM, "": 0}
        tab, top, paused = start_tab, 0, False
        snap = None
        while True:
            h, w = scr.getmaxyx()
            if not paused or snap is None:
                try:
                    snap = snap_mod.build(paths)
                except sqlite3.Error as e:
                    snap = snap or None
                    scr.erase(); scr.addnstr(0, 0, f"db busy/unavailable: {e}", w - 1, sty["bad"]); scr.refresh()
                    if scr.getch() in (ord("q"), 27):
                        return
                    continue
            lines = render(snap, TABS[tab], w - 1)
            scr.erase()
            bar = " ".join(f"[{i + 1}]{n}" if i == tab else f" {i + 1} {n}" for i, n in enumerate(TABS))
            scr.addnstr(0, 0, bar.ljust(w - 1), w - 1, curses.A_REVERSE)
            top = max(0, min(top, max(0, len(lines) - (h - 2))))
            for i, (t, st) in enumerate(lines[top:top + h - 2]):
                scr.addnstr(i + 1, 0, t, w - 1, sty.get(st, 0))
            scr.addnstr(h - 1, 0, (" 1-6/←→ tabs  j/k scroll  p " + ("RESUME" if paused else "pause") + "  q quit  ").ljust(w - 1), w - 1, curses.A_REVERSE)
            scr.refresh()
            k = scr.getch()
            if k in (ord("q"), 27):
                return
            if ord("1") <= k <= ord("6"):
                tab, top = k - ord("1"), 0
            elif k in (curses.KEY_RIGHT, ord("l"), 9):
                tab, top = (tab + 1) % len(TABS), 0
            elif k in (curses.KEY_LEFT, ord("h")):
                tab, top = (tab - 1) % len(TABS), 0
            elif k in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif k in (curses.KEY_UP, ord("k")):
                top -= 1
            elif k == ord("p"):
                paused = not paused

    curses.wrapper(main)
