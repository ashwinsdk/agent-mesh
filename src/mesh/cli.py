"""mesh command line."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import adapters, bus, checkpoint, cli2, context, core, gitflow, hooks, locks, memory, registry, tasks, tui, usage
from .db import DB, MeshError, Paths, atomic_write, find_root, now, sha256

DEFAULT_TOML = """# mesh configuration (committed). Edit freely.
[project]
name = "{name}"
profile = "{profile}"   # detected: {manifests}

[git]
integration_base = ""            # empty = auto (main/master)
integration_branch = "mesh/integration"

[validation]
test = "{test}"                  # run before and after every integration
lint = "{lint}"

[locks]
ttl_seconds = 900

[tasks]
stale_after_seconds = 1800       # silent this long => task considered abandoned

[context]
warn_pct = 70
checkpoint_pct = 85
brief_budget_chars = 6000

[policy]
destructive = "deny"             # deny | allow
secret_in_write = "deny"         # deny | allow
protect_mesh_files = true
"""

SEED_PROJECT = """# Project memory
<!-- Canonical shared facts. Add via `mesh mem fact`; every fact is tagged verified|assumed with date and author. -->
<!-- Source code and config always outrank this file. -->

## Overview
(describe the project: `mesh mem fact --status verified --source <file> "..."`)

## Facts
"""


def _tbl(rows, cols):
    if not rows:
        return "(none)"
    w = [max(len(c), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    out = ["  ".join(c.ljust(w[i]) for i, c in enumerate(cols))]
    out += ["  ".join(str(r[i]).ljust(w[i]) for i in range(len(cols))) for r in rows]
    return "\n".join(out)


def p_out(x):
    print(json.dumps(x, indent=2, default=str))


def build_parser():
    ap = argparse.ArgumentParser(prog="mesh", description="Multi-agent coordination for any repository")
    ap.add_argument("--as", dest="actor", help="acting session id (default: $MESH_SESSION)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("--no-sync", action="store_true"); s.add_argument("--name")
    s = sub.add_parser("sync", help="regenerate client config from .mesh"); s.add_argument("--cli", action="append")
    s = sub.add_parser("upgrade", help="refresh vendored .mesh/lib from this install")
    s = sub.add_parser("doctor"); s.add_argument("--repair", action="store_true")

    s = sub.add_parser("session"); ss = s.add_subparsers(dest="sub", required=True)
    x = ss.add_parser("start"); x.add_argument("--cli", required=True); x.add_argument("--agent"); x.add_argument("--native-id"); x.add_argument("--model")
    ss.add_parser("end"); ss.add_parser("list"); ss.add_parser("heartbeat")

    s = sub.add_parser("task"); ts = s.add_subparsers(dest="sub", required=True)
    x = ts.add_parser("create"); x.add_argument("title"); x.add_argument("-d", "--description", default="")
    x.add_argument("--parent"); x.add_argument("--kind"); x.add_argument("-p", "--priority", type=int, default=2)
    x.add_argument("--dep", action="append", default=[]); x.add_argument("--files", default="", help="comma-separated paths/globs")
    x.add_argument("--accept", default=""); x.add_argument("--agent"); x.add_argument("--backlog", action="store_true"); x.add_argument("--id")
    x.add_argument("--verify", default="", help="shell command that proves the task is done"); x.add_argument("--risk", default="low", choices=["low", "high"])
    x = ts.add_parser("list"); x.add_argument("--status"); x.add_argument("--all", action="store_true")
    for n in ("show", "start", "review", "complete", "release", "resume", "pause", "cancel", "tree"):
        x = ts.add_parser(n); x.add_argument("id", nargs="?")
    x = ts.add_parser("submit", help="worker: submit finished work for review (cannot complete a task)"); x.add_argument("id"); x.add_argument("--note", default="")
    x = ts.add_parser("dep"); x.add_argument("id"); x.add_argument("on")
    x = ts.add_parser("ready"); x.add_argument("--agent")
    x = ts.add_parser("claim"); x.add_argument("id"); x.add_argument("--agent"); x.add_argument("--cli")
    for n in ("block", "fail", "rework"):
        x = ts.add_parser(n); x.add_argument("id"); x.add_argument("--note", default="")
    x = ts.add_parser("reap"); x.add_argument("--stale-after", type=float)
    x = ts.add_parser("plan", help="suggest agents"); x.add_argument("title"); x.add_argument("-d", "--description", default="")

    s = sub.add_parser("lock"); ls = s.add_subparsers(dest="sub", required=True)
    x = ls.add_parser("acquire"); x.add_argument("patterns", nargs="+"); x.add_argument("--task")
    x = ls.add_parser("release"); x.add_argument("patterns", nargs="*")
    ls.add_parser("list"); x = ls.add_parser("check"); x.add_argument("path")

    s = sub.add_parser("msg"); ms = s.add_subparsers(dest="sub", required=True)
    x = ms.add_parser("send"); x.add_argument("content"); x.add_argument("--to", required=True); x.add_argument("--type", default="info")
    x.add_argument("--task"); x.add_argument("--priority", type=int, default=2); x.add_argument("--dedupe")
    x.add_argument("--requires-ack", action="store_true"); x.add_argument("--ttl", type=float)
    x = ms.add_parser("inbox"); x.add_argument("--all", action="store_true")
    x = ms.add_parser("ack"); x.add_argument("id", nargs="+")

    s = sub.add_parser("mem"); mm = s.add_subparsers(dest="sub", required=True)
    x = mm.add_parser("show"); x.add_argument("key", nargs="?", default="project")
    x = mm.add_parser("fact"); x.add_argument("text"); x.add_argument("--status", default="assumed"); x.add_argument("--source", default="")
    x = mm.add_parser("adr"); x.add_argument("title"); x.add_argument("--context", required=True); x.add_argument("--decision", required=True); x.add_argument("--consequences", default="")
    x = mm.add_parser("entry"); x.add_argument("kind"); x.add_argument("text")
    x = mm.add_parser("commit"); x.add_argument("key"); x.add_argument("--file", required=True); x.add_argument("--expect", required=True)
    x = mm.add_parser("history"); x.add_argument("key", nargs="?", default="project")
    x = mm.add_parser("restore"); x.add_argument("key"); x.add_argument("snapshot")
    mm.add_parser("stale"); mm.add_parser("working")

    s = sub.add_parser("checkpoint"); cs = s.add_subparsers(dest="sub", required=True)
    x = cs.add_parser("create"); x.add_argument("--task")
    for f in ("done", "decision", "command", "test", "issue", "next"):
        x.add_argument(f"--{f}", action="append", default=[])
    x.add_argument("--stage", default=""); x.add_argument("--reason", default="manual")
    x = cs.add_parser("show"); x.add_argument("--task"); x = cs.add_parser("list")
    x = sub.add_parser("resume"); x.add_argument("task", nargs="?")
    x = sub.add_parser("rollover"); x.add_argument("--task"); x.add_argument("--launch", metavar="CLI")

    s = sub.add_parser("context"); cx = s.add_subparsers(dest="sub", required=True)
    x = cx.add_parser("brief"); x.add_argument("task"); x.add_argument("--budget", type=int)
    x = cx.add_parser("eval"); x.add_argument("--pct", type=float, required=True); x.add_argument("--task"); x.add_argument("--source", default="estimated")

    s = sub.add_parser("worktree"); ws = s.add_subparsers(dest="sub", required=True)
    x = ws.add_parser("create"); x.add_argument("task"); x = ws.add_parser("remove"); x.add_argument("task")
    x = sub.add_parser("integrate"); x.add_argument("task"); sub.add_parser("promote")

    s = sub.add_parser("usage"); us = s.add_subparsers(dest="sub", required=True)
    us.add_parser("report")
    x = us.add_parser("record"); x.add_argument("--cli", required=True); x.add_argument("--source", default="estimated")
    for f in ("input_tokens", "output_tokens", "context_pct", "context_tokens"):
        x.add_argument("--" + f.replace("_", "-"), type=float)
    x.add_argument("--transcript")
    x = us.add_parser("ratelimit"); x.add_argument("--cli", required=True); x.add_argument("--detail", default="")

    s = sub.add_parser("agents"); ag = s.add_subparsers(dest="sub", required=True)
    ag.add_parser("list"); x = ag.add_parser("show"); x.add_argument("id")

    x = sub.add_parser("hook"); x.add_argument("cli"); x.add_argument("event")
    sub.add_parser("statusline")
    x = sub.add_parser("dash"); x.add_argument("--once", action="store_true"); x.add_argument("--interval", type=float, default=1.0)
    x.add_argument("--tab", help="overview|agents|sessions|tasks|comms|usage (with --once prints only that tab)")
    x.add_argument("--web", action="store_true", help="serve the read-only web viewer on 127.0.0.1"); x.add_argument("--port", type=int, default=8765)
    x = sub.add_parser("backup"); x = sub.add_parser("restore"); x.add_argument("file", nargs="?")
    x = sub.add_parser("gc"); x.add_argument("--keep-days", type=int, default=30)
    x = sub.add_parser("run", help="launch a CLI inside a mesh session (fallback for CLIs lacking SessionEnd)")
    x.add_argument("client"); x.add_argument("--task"); x.add_argument("--agent"); x.add_argument("--prompt", default="")
    cli2.add_parsers(sub)
    return ap


def _hoist_global_flags(argv: list[str]) -> list[str]:
    """Allow `--as X` / `--json` anywhere on the command line."""
    out, front, i = [], [], 0
    while i < len(argv):
        if argv[i] == "--as" and i + 1 < len(argv):
            front += argv[i:i + 2]; i += 2
        elif argv[i].startswith("--as="):
            front.append(argv[i]); i += 1
        elif argv[i] == "--json":
            front.append(argv[i]); i += 1
        else:
            out.append(argv[i]); i += 1
    return front + out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("hook", "statusline"):
        argv = _hoist_global_flags(argv)
    args = build_parser().parse_args(argv)
    try:
        return dispatch(args) or 0
    except MeshError as e:
        print(f"mesh: error: {e}", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as e:
        print(f"mesh: database error: {e}. Try `mesh doctor --repair`.", file=sys.stderr)
        return 2


def dispatch(a) -> int:
    c = a.cmd
    if c == "hook":
        return hooks.run(a.cli, a.event)
    if c == "statusline":
        return hooks.statusline()
    if c == "init":
        return cmd_init(a)
    paths = Paths(find_root())
    if c == "restore":
        return cmd_restore(paths, a)
    db = DB(paths)
    try:
        actor = core.resolve_actor(db, a.actor)
        fn = globals().get(f"cmd_{c}") or cli2.HANDLERS[c.replace("-", "_")]
        return fn(db, a, actor)
    finally:
        db.close()


# ------------------------------------------------------------------ init / sync / doctor

def cmd_init(a) -> int:
    root = Path(os.environ.get("MESH_ROOT") or os.getcwd()).resolve()
    paths = Paths(root)
    for d in (paths.memory / "decisions", paths.agents, paths.runtime, paths.checkpoints, paths.backups, paths.logs, paths.mesh / "instructions"):
        d.mkdir(parents=True, exist_ok=True)
    det = registry.detect(root)
    if not paths.config.exists():
        atomic_write(paths.config, DEFAULT_TOML.format(name=a.name or root.name, profile=det["profile"],
                                                        manifests=",".join(det["manifests"]) or "none", test=det["test"], lint=det["lint"]))
    if not (paths.memory / "project.md").exists():
        atomic_write(paths.memory / "project.md", SEED_PROJECT)
    for f, title in (("lessons.md", "Lessons learned"), ("rejected.md", "Rejected approaches"), ("debt.md", "Technical debt")):
        if not (paths.memory / f).exists():
            atomic_write(paths.memory / f, f"# {title}\n\n")
    atomic_write(paths.mesh / "instructions" / "CORE.md", adapters.CORE)
    new_agents = registry.install_agents(paths.agents)
    adapters.vendor_lib(paths)
    gi = root / ".gitignore"
    line = ".mesh/runtime/"
    cur = gi.read_text() if gi.exists() else ""
    if line not in cur.splitlines():
        atomic_write(gi, cur.rstrip("\n") + ("\n" if cur else "") + "# mesh runtime state (local only)\n" + line + "\n.mesh/lib/**/__pycache__/\n*.mesh-proposed\n")
    db = DB(paths)
    db.event("human", "init", None, det)
    db.close()
    print(f"mesh initialised in {root}\n  profile: {det['profile']}  manifests: {', '.join(det['manifests']) or '-'}")
    print(f"  validation.test = {det['test'] or '(none detected; set in .mesh/mesh.toml)'}")
    print(f"  agents installed: {len(new_agents)} new")
    if not a.no_sync:
        for l in adapters.sync_all(paths, ["claude", "copilot", "antigravity"]):
            print("  " + l)
    print("Next: `.mesh/bin/mesh doctor`, then commit .mesh/ (runtime/ is ignored).")
    return 0


def cmd_sync(db, a, actor):
    from . import playbooks
    for l in adapters.sync_all(db.paths, a.cli):
        print(l)
    atomic_write(db.paths.mesh / "instructions" / "ORCHESTRATOR.md", playbooks.ORCHESTRATOR.replace("{session}", "<session>").replace("{plan_approval}", ""))
    return 0


def cmd_upgrade(db, a, actor):
    print(adapters.vendor_lib(db.paths))
    return 0


def cmd_doctor(db, a, actor):
    ok = True
    print("== Environment")
    ig = db.integrity()
    print(f"database integrity: {ig}")
    ok &= ig == "ok"
    print(f"git: {'yes' if core.is_git(db.paths.root) else 'NO (worktrees/integration disabled)'}")
    cfg = core.load_config(db.paths)
    print(f"validation.test: {cfg['validation']['test'] or '(not configured: integration will skip tests)'}")
    from . import workers as _w
    sv = db.one("SELECT value FROM meta WHERE key='schema_version'")[0]
    print(f"schema version: {sv}")
    cfg2 = core.load_config(db.paths)
    print(f"autonomy accepted: {'yes by ' + cfg2['autonomy']['accepted_by'] + ' at ' + cfg2['autonomy']['accepted_at'] if cfg2['autonomy']['accepted_at'] else 'NO (needed before workers can be spawned: mesh autonomy accept)'}")
    try:
        _w.check_committed(db, cfg2)
        print("mesh config committed on base branch: yes")
    except MeshError as e:
        print(f"WARNING {e}")
    print(f"workers: {_w.active_count(db)} active; reconciled: {_w.reconcile(db) or 'consistent'}")
    errlog = db.paths.logs / "hook-errors.log"
    if errlog.exists():
        print(f"WARNING hook errors logged: {errlog} ({errlog.stat().st_size} bytes)")
    print(f"expired locks recovered: {locks.recover_dead(db)}")
    stale = memory.stale_report(db)
    print(f"memory staleness warnings: {len(stale)}")
    for s in stale[:5]:
        print("  - " + s)
    print("\n== Capability matrix (native = vendor-documented hook/feature used; verify = run `mesh doctor --repair` after live test)")
    feats = ["instructions", "parallel_agents", "hooks", "context_visibility", "token_usage", "quota", "session_recovery"]
    rows = []
    for f in feats:
        rows.append([f] + [adapters.ADAPTERS[n](db.paths).CAPS.get(f, ("-", ""))[0] for n in ("claude", "antigravity", "copilot")])
    def observed(cli):
        n = db.one("SELECT COUNT(*) FROM events e JOIN sessions s ON s.id=e.actor WHERE s.cli=? AND e.type IN ('tool','session.start')", (cli,))[0]
        pre = db.one("SELECT COUNT(*) FROM sessions WHERE cli=?", (cli,))[0]
        return f"yes ({pre} sess)" if n and pre else "NOT YET (run tests/live/verify_cli.sh)"
    rows.append(["hooks seen live here", *[observed(n) for n in ("claude", "antigravity", "copilot")]])
    rows.append(["external coordination", "common layer", "common layer", "common layer"])
    print(_tbl(rows, ["feature", "claude", "antigravity", "copilot"]))
    print("\n== Installed clients")
    for n, cls in adapters.ADAPTERS.items():
        if n != "generic":
            print(f"{n:12} {cls(db.paths).available() or 'not found on PATH'}")
    if a.repair and ig != "ok":
        return cmd_restore(db.paths, argparse.Namespace(file=None))
    return 0 if ok else 1


# ------------------------------------------------------------------ session

def cmd_session(db, a, actor):
    if a.sub == "start":
        sid = core.register_session(db, a.cli, a.agent, a.native_id, os.getppid(), a.model)
        print(sid)
        print(f"# use: export MESH_SESSION={sid}  (or pass --as {sid})", file=sys.stderr)
    elif a.sub == "end":
        core.end_session(db, actor)
    elif a.sub == "heartbeat":
        core.heartbeat(db, actor); locks.heartbeat(db, actor)
    else:
        rows = db.q("SELECT * FROM sessions ORDER BY last_seen DESC")
        print(_tbl([[r["id"], r["cli"], r["agent"] or "-", r["status"], r["task_id"] or "-", f"{int(now() - r['last_seen'])}s"] for r in rows],
                   ["session", "cli", "agent", "status", "task", "last_seen"]))
    return 0


# ------------------------------------------------------------------ tasks

def _fmt_task(r):
    return [r["id"], r["kind"], r["status"], r["priority"], r["session_id"] or "-", r["agent"] or "-", r["title"][:50]]


def cmd_task(db, a, actor):
    s = a.sub
    cfg = core.load_config(db.paths)
    if s == "create":
        tid = tasks.create(db, a.title, a.description, a.parent, a.kind, a.priority, a.dep,
                           [f for f in a.files.split(",") if f], a.accept, a.agent, a.backlog, actor, a.id,
                           verify_cmd=a.verify, risk=a.risk)
        print(tid)
    elif s == "list":
        q = "SELECT * FROM tasks WHERE kind!='project'" + ("" if a.all else " AND status NOT IN ('COMPLETED','CANCELLED')")
        args = ()
        if a.status:
            q += " AND status=?"; args = (a.status,)
        print(_tbl([_fmt_task(r) for r in db.q(q + " ORDER BY priority, created", args)], ["id", "kind", "status", "prio", "owner", "agent", "title"]))
    elif s == "show":
        t = tasks.get(db, a.id)
        d = dict(t); d["deps"] = [r[0] for r in db.q("SELECT dep_id FROM task_deps WHERE task_id=?", (a.id,))]
        p_out(d)
    elif s == "ready":
        print(_tbl([_fmt_task(r) for r in tasks.ready(db, a.agent)], ["id", "kind", "status", "prio", "owner", "agent", "title"]))
    elif s == "claim":
        t = tasks.claim(db, a.id, actor, a.agent, a.cli or (db.one("SELECT cli FROM sessions WHERE id=?", (actor,)) or [None])[0], cfg["locks"]["ttl_seconds"])
        print(f"claimed {t['id']} as {actor}; locks acquired on {t['files']}")
    elif s == "submit":
        from . import verify
        print(verify.submit(db, a.id, actor, a.note))
    elif s == "dep":
        tasks.add_dep(db, a.id, a.on)
    elif s == "start":
        tasks.transition(db, a.id, "IN_PROGRESS", actor)
    elif s == "review":
        from . import verify
        print(verify.submit(db, a.id, actor, ""))
    elif s == "complete":
        cli2.require_privileged(db, actor)
        tasks.transition(db, a.id, "COMPLETED", actor)
    elif s == "release":
        tasks.transition(db, a.id, "READY", actor)
    elif s == "pause":
        tasks.transition(db, a.id, "PAUSED", actor)
    elif s == "cancel":
        tasks.transition(db, a.id, "CANCELLED", actor)
    elif s in ("block", "fail", "rework"):
        tasks.transition(db, a.id, {"block": "BLOCKED", "fail": "FAILED", "rework": "NEEDS_REWORK"}[s], actor, a.note)
    elif s == "resume":
        t = tasks.resume(db, a.id, actor, cfg["tasks"]["stale_after_seconds"], cfg["locks"]["ttl_seconds"])
        print(f"{actor} now owns {t['id']} ({t['status']}). Run: mesh resume {t['id']}")
    elif s == "reap":
        print("reassigned:", tasks.reap(db, a.stale_after or cfg["tasks"]["stale_after_seconds"]) or "none")
    elif s == "tree":
        def walk(pid, ind):
            for r in db.q("SELECT * FROM tasks WHERE parent_id IS ? ORDER BY created", (pid,)) if pid is None else tasks.children(db, pid):
                print("  " * ind + f"{r['id']} [{r['status']}] {r['kind']}: {r['title']} ({tasks.progress(db, r['id']) * 100:.0f}%)")
                walk(r["id"], ind + 1)
        walk(a.id, 0)
    elif s == "plan":
        print("suggested agents:", ", ".join(registry.suggest(a.title, a.description)))
    return 0


def cmd_lock(db, a, actor):
    cfg = core.load_config(db.paths)
    if a.sub == "acquire":
        bad = locks.acquire(db, actor, a.patterns, a.task, cfg["locks"]["ttl_seconds"])
        if bad:
            print("DENIED:", *[f"{b['pattern']} overlaps {b['held']} (owner {b['owner']})" for b in bad], sep="\n  ")
            return 3
        print("acquired")
    elif a.sub == "release":
        print(f"released {locks.release(db, actor, a.patterns or None)}")
    elif a.sub == "check":
        b = locks.check_path(db, actor, a.path)
        print(f"LOCKED by {b['owner']} (task {b['task_id']})" if b else "free")
        return 3 if b else 0
    else:
        print(_tbl([[r["pattern"], r["owner"], r["task_id"] or "-", f"{int(r['expires'] - now())}s"] for r in db.q("SELECT * FROM locks")],
                   ["pattern", "owner", "task", "expires_in"]))
    return 0


def cmd_msg(db, a, actor):
    if a.sub == "send":
        mid = bus.send(db, actor, a.to, a.type, a.content, a.task, a.priority, a.dedupe, requires_ack=a.requires_ack, ttl=a.ttl)
        print(mid or "duplicate (dedupe key exists); not sent")
    elif a.sub == "inbox":
        rows = bus.inbox(db, actor, a.all)
        print(bus.render_untrusted(rows) if rows else "(no unread messages)")
    else:
        for m in a.id:
            bus.ack(db, actor, m)
    return 0


def cmd_mem(db, a, actor):
    s = a.sub
    if s == "show":
        t, h = memory.read(db, a.key)
        print(memory.BANNER + t + f"\n<!-- sha256 {h} -->")
    elif s == "fact":
        memory.add_fact(db, a.text, actor, a.status, a.source)
    elif s == "adr":
        print(memory.new_adr(db, a.title, a.context, a.decision, actor, a.consequences))
    elif s == "entry":
        memory.add_entry(db, a.kind, a.text, actor)
    elif s == "commit":
        print("version", memory.commit(db, a.key, Path(a.file).read_text(), actor, a.expect))
    elif s == "history":
        print("\n".join(str(p) for p in memory.history(db, a.key)) or "(no snapshots)")
    elif s == "restore":
        memory.restore(db, a.key, Path(a.snapshot), actor)
    elif s == "stale":
        print("\n".join(memory.stale_report(db)) or "no staleness warnings")
    elif s == "working":
        t = memory.working_memory(db)
        atomic_write(db.paths.runtime / "working.md", t)
        print(t)
    return 0


def cmd_checkpoint(db, a, actor):
    if a.sub == "create":
        row = db.one("SELECT task_id FROM sessions WHERE id=?", (actor,))
        tid = a.task or (row["task_id"] if row else None)
        cid = checkpoint.create(db, actor, tid, {"completed_steps": a.done, "decisions": a.decision, "commands": a.command, "tests": a.test,
                                                  "issues": a.issue, "next_actions": a.next, "current_stage": a.stage}, a.reason)
        print(cid)
    elif a.sub == "show":
        r = checkpoint.latest_valid(db, a.task)
        if not r:
            print("no valid checkpoint"); return 1
        p_out({"id": r[0], **r[1]})
    else:
        print(_tbl([[r["id"], r["task_id"] or "-", r["session_id"], r["cli"], r["reason"], f"{int(now() - r['created'])}s ago"]
                    for r in db.q("SELECT * FROM checkpoints ORDER BY created DESC LIMIT 30")], ["id", "task", "session", "cli", "reason", "age"]))
    return 0


def cmd_resume(db, a, actor):
    cfg = core.load_config(db.paths)
    tid = a.task
    if not tid:
        ab = checkpoint.interrupted(db, cfg["tasks"]["stale_after_seconds"])
        if not ab:
            print("No interrupted tasks.")
            return 0
        print("Interrupted tasks:\n" + _tbl([[r["id"], r["title"][:50], r["session_id"], r["status"]] for r in ab], ["id", "title", "prev owner", "status"]))
        print("\nContinue one with: mesh task resume <id> && mesh resume <id>")
        return 0
    print(checkpoint.brief(db, tid, actor, cfg["context"]["brief_budget_chars"]))
    return 0


def cmd_rollover(db, a, actor):
    row = db.one("SELECT task_id, cli FROM sessions WHERE id=?", (actor,))
    tid = a.task or (row["task_id"] if row else None)
    cid = checkpoint.create(db, actor, tid, {"current_stage": "context rollover requested"}, reason="rollover")
    prompt = context.rollover_prompt(db, tid)
    core.end_session(db, actor, "rolled-over")
    db.event(actor, "context.rollover", tid, {"checkpoint": cid})
    if a.launch:
        cmd = adapters.ADAPTERS[a.launch](db.paths).launch_cmd(prompt)
        print(f"launching: {cmd[0]} (seeded with continuation brief)", file=sys.stderr)
        os.execvp(cmd[0], cmd)
    print(prompt)
    return 0


def cmd_context(db, a, actor):
    if a.sub == "brief":
        print(context.brief(db, a.task, actor, a.budget))
    else:
        row = db.one("SELECT task_id FROM sessions WHERE id=?", (actor,))
        p_out(context.evaluate(db, actor, a.task or (row["task_id"] if row else None), a.pct, a.source))
    return 0


def cmd_worktree(db, a, actor):
    if a.sub == "create":
        print(gitflow.worktree_create(db, a.task))
    else:
        gitflow.worktree_remove(db, a.task)
    return 0


def cmd_integrate(db, a, actor):
    cli2.require_privileged(db, actor)
    r = gitflow.integrate(db, a.task, actor)
    print(("OK: " if r["ok"] else f"FAILED at {r['stage']}: ") + r["detail"])
    return 0 if r["ok"] else 4


def cmd_promote(db, a, actor):
    cli2.require_privileged(db, actor)
    print(gitflow.promote(db))
    return 0


def cmd_usage(db, a, actor):
    if a.sub == "report":
        p_out(usage.report(db))
    elif a.sub == "ratelimit":
        usage.record_rate_limit(db, actor, a.cli, a.detail)
    else:
        m = {k: getattr(a, k) for k in ("input_tokens", "output_tokens", "context_pct", "context_tokens")}
        if a.transcript:
            est = usage.estimate_from_file(a.transcript)
            if est:
                m.update(est); a.source = "estimated"
        row = db.one("SELECT task_id, agent FROM sessions WHERE id=?", (actor,))
        usage.record(db, actor, a.cli, a.source, task_id=row["task_id"] if row else None, agent=row["agent"] if row else None, **m)
    return 0


def cmd_agents(db, a, actor):
    ag = registry.load_agents(db.paths.agents)
    if a.sub == "list":
        print(_tbl([[x["id"], x["role"], x.get("capabilities", "")[:60]] for x in ag], ["id", "role", "capabilities"]))
    else:
        x = next((x for x in ag if x["id"] == a.id), None)
        if not x:
            raise MeshError("unknown agent")
        print((db.paths.agents / f"{a.id}.md").read_text())
    return 0


def cmd_dash(db, a, actor):
    from . import snapshot
    if a.web:
        from . import webdash
        webdash.serve(db.paths, a.port)
        return 0
    if getattr(a, "json", False):
        print(json.dumps(snapshot.build(db.paths), indent=2, default=str))
    elif a.once or not sys.stdout.isatty():
        if a.tab:
            snap = snapshot.build(db.paths)
            name = next((t for t in tui.TABS if t.lower() == a.tab.lower()), None)
            if not name:
                raise MeshError(f"tab must be one of {', '.join(t.lower() for t in tui.TABS)}")
            print("\n".join(t for t, _ in tui.render(snap, name)))
        else:
            print(tui.render_all(db.paths))
    else:
        db.close()
        start = next((i for i, t in enumerate(tui.TABS) if a.tab and t.lower() == a.tab.lower()), 0)
        tui.live(db.paths, a.interval, start)
    return 0


def cmd_backup(db, a, actor):
    db.paths.backups.mkdir(parents=True, exist_ok=True)
    dst = db.paths.backups / f"mesh-{int(time.time())}.db"
    b = sqlite3.connect(dst)
    db.conn.backup(b)
    b.close()
    print(dst)
    return 0


def cmd_restore(paths: Paths, a) -> int:
    cands = [Path(a.file)] if a.file else sorted(paths.backups.glob("mesh-*.db"), reverse=True)
    for f in cands:
        try:
            t = sqlite3.connect(f)
            ok = t.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            t.close()
        except sqlite3.DatabaseError:
            ok = False
        if ok:
            for ext in ("", "-wal", "-shm"):
                Path(str(paths.db) + ext).unlink(missing_ok=True)
            shutil.copy2(f, paths.db)
            print(f"restored from {f}")
            return 0
    print("no valid backup found", file=sys.stderr)
    return 1


def cmd_gc(db, a, actor):
    cutoff = now() - a.keep_days * 86400
    with db.tx() as c:
        n1 = c.execute("DELETE FROM events WHERE ts<?", (cutoff,)).rowcount
        n2 = c.execute("DELETE FROM usage WHERE ts<?", (cutoff,)).rowcount
    n3 = 0
    runs = db.paths.runtime / "runs"
    if runs.exists():
        live = {w["id"] for w in db.q("SELECT id FROM workers WHERE ended IS NULL")}
        for d in runs.iterdir():
            if d.name not in live and d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True); n3 += 1
    print(f"pruned {n1} events, {n2} usage rows older than {a.keep_days}d; removed {n3} finished worker run dirs")
    return 0


def cmd_run(db, a, actor):
    """Wrapper launcher: gives CLIs without SessionEnd hooks (Antigravity) reliable lifecycle + crash checkpoints."""
    cls = adapters.ADAPTERS.get(a.client)
    if not cls or a.client == "generic":
        raise MeshError(f"unknown client {a.client}")
    ad = cls(db.paths)
    if not ad.available():
        raise MeshError(f"{ad.binary} not found on PATH")
    sid = core.register_session(db, a.client, a.agent, None, os.getpid())
    env = {**os.environ, "MESH_SESSION": sid, "MESH_AGENT": a.agent or ""}
    prompt = a.prompt
    if a.task:
        cfg = core.load_config(db.paths)
        tasks.resume(db, a.task, sid, cfg["tasks"]["stale_after_seconds"], cfg["locks"]["ttl_seconds"], allow_ready=True)
        prompt = checkpoint.brief(db, a.task, sid) + "\n\n" + prompt
    cmd = ad.launch_cmd(prompt or f"You are mesh session {sid}. Read AGENTS.md and begin with `.mesh/bin/mesh task ready`.")
    code = subprocess.call(cmd, env=env, cwd=db.paths.root)
    row = db.one("SELECT task_id FROM sessions WHERE id=?", (sid,))
    if row and row["task_id"]:
        checkpoint.create(db, sid, row["task_id"], {"current_stage": f"CLI exited with code {code}"}, reason="wrapper-exit")
    core.end_session(db, sid, "ended" if code == 0 else "crashed")
    return code
