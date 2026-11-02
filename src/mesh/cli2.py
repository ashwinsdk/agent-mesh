"""v2 commands: workers, halt, autonomy, commit-config, wait/tail, verification gates, up, report."""
from __future__ import annotations

import getpass
import json
import os
import sys
import time
from pathlib import Path

from . import adapters, bus, checkpoint, core, eventlog, gitflow, memory, playbooks, registry, tasks, usage, verify, workers
from .core import load_config
from .db import DB, MeshError, atomic_write, now


def require_privileged(db: DB, actor: str) -> None:
    """Workers may never approve, integrate, spawn or halt. Humans and orchestrator sessions may."""
    row = db.one("SELECT role FROM sessions WHERE id=?", (actor,))
    if row and row["role"] == "worker":
        raise MeshError(f"'{actor}' is a worker session; this action is reserved for the orchestrator/human")


def _tbl(rows, cols):
    from .cli import _tbl as t
    return t(rows, cols)


def add_parsers(sub):
    s = sub.add_parser("worker"); ws = s.add_subparsers(dest="sub", required=True)
    x = ws.add_parser("spawn"); x.add_argument("--task", required=True); x.add_argument("--agent"); x.add_argument("--cli"); x.add_argument("--model")
    ws.add_parser("list"); x = ws.add_parser("show"); x.add_argument("id")
    x = ws.add_parser("send"); x.add_argument("id"); x.add_argument("text")
    x = ws.add_parser("terminate"); x.add_argument("id"); x.add_argument("--reason", default="terminated by orchestrator")
    x = ws.add_parser("restart"); x.add_argument("id"); x.add_argument("--feedback")
    ws.add_parser("reconcile")
    x = ws.add_parser("run"); x.add_argument("id")  # internal
    x = ws.add_parser("request"); x.add_argument("--task", required=True); x.add_argument("--agent", required=True); x.add_argument("--reason", default="")
    x = ws.add_parser("approve-spawn"); x.add_argument("approval", type=int); x.add_argument("--reject", action="store_true")
    sub.add_parser("halt"); sub.add_parser("unhalt")
    s = sub.add_parser("autonomy"); a = s.add_subparsers(dest="sub", required=True)
    x = a.add_parser("accept"); x.add_argument("--yes", action="store_true"); a.add_parser("show")
    sub.add_parser("commit-config")
    x = sub.add_parser("wait", help="block until important events arrive (event-driven supervision)")
    x.add_argument("--consumer", default="orchestrator"); x.add_argument("--timeout", type=float, default=600)
    x.add_argument("--all", action="store_true", help="include noisy event types"); x.add_argument("--peek", action="store_true")
    x = sub.add_parser("tail"); x.add_argument("--since", type=int, default=0); x.add_argument("--limit", type=int, default=50); x.add_argument("--all", action="store_true")
    x = sub.add_parser("submit"); x.add_argument("task"); x.add_argument("--note", default="")
    x = sub.add_parser("verify"); x.add_argument("task")
    x = sub.add_parser("approve"); x.add_argument("task"); x.add_argument("--accept-unverified", action="store_true"); x.add_argument("--integrate", action="store_true")
    x = sub.add_parser("reject"); x.add_argument("task"); x.add_argument("--reason", required=True)
    x = sub.add_parser("diff"); x.add_argument("task")
    s = sub.add_parser("review"); r = s.add_subparsers(dest="sub", required=True)
    for n in ("approve", "reject"):
        x = r.add_parser(n); x.add_argument("task"); x.add_argument("--note", default="")
    x = sub.add_parser("decide"); x.add_argument("message"); x.add_argument("verdict", choices=["accept", "reject"]); x.add_argument("--note", default="")
    x = sub.add_parser("dep"); x.add_argument("task"); x.add_argument("on")
    x = sub.add_parser("plan"); x.add_argument("what", nargs="?", default="show")
    x = sub.add_parser("reply"); x.add_argument("message"); x.add_argument("text")
    x = sub.add_parser("up", help="start the primary orchestrator session in the chosen CLI")
    x.add_argument("client", choices=["claude", "copilot", "antigravity"]); x.add_argument("prompt", nargs="?", default="")
    x.add_argument("--resume", action="store_true"); x.add_argument("--accept-autonomy", action="store_true")
    x.add_argument("--dry-run", action="store_true")
    sub.add_parser("report")


# ------------------------------------------------------------------ workers

def _wrow(w):
    return [w["id"], w["agent"] or "-", f"{w['cli']}/{w['model'] or '-'}", w["state"], w["task_id"] or "-", w["pid"] or "-",
            w["restarts"], f"{int(now() - (w['last_output'] or w['started']))}s"]


def cmd_worker(db, a, actor):
    s = a.sub
    if s == "run":
        from . import runner
        db.close()
        return runner.run(a.id)
    if s == "request":
        # a worker asks for help; only the orchestrator can approve (hierarchical delegation under control)
        me = db.one("SELECT * FROM workers WHERE id=?", (actor,))
        depth = (me["depth"] + 1) if me else 1
        with db.tx() as c:
            c.execute("INSERT INTO approvals(kind,subject,requested_by,payload,ts) VALUES('spawn',?,?,?,?)",
                      (a.task, actor, json.dumps({"agent": a.agent, "reason": a.reason, "depth": depth, "parent": actor}), now()))
            aid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        bus.send(db, actor, "role:orchestrator", "spawn_request", f"approval #{aid}: {actor} requests a '{a.agent}' worker for task {a.task}. {a.reason}",
                 a.task, 1, requires_ack=True)
        print(f"approval #{aid} requested")
        return 0
    require_privileged(db, actor)
    if s == "spawn":
        w = workers.spawn(db, a.task, a.agent, a.cli, a.model, actor)
        print(f"{w['id']} spawned ({w['cli']}/{w['model'] or 'default'}) for task {a.task} in {w['worktree']}")
    elif s == "list":
        workers.reconcile(db)
        print(_tbl([_wrow(w) for w in workers.list_workers(db)], ["worker", "agent", "cli/model", "state", "task", "pid", "restarts", "silent"]))
    elif s == "show":
        w = workers.get(db, a.id)
        d = dict(w)
        d["assignments"] = [dict(r) for r in db.q("SELECT * FROM assignments WHERE worker_id=?", (a.id,))]
        d["latest_usage"] = dict(usage.latest(db, a.id)) if usage.latest(db, a.id) else None
        print(json.dumps(d, indent=2, default=str))
    elif s == "send":
        print(workers.send(db, a.id, a.text, actor))
    elif s == "terminate":
        workers.terminate(db, a.id, actor, a.reason)
    elif s == "restart":
        workers.restart(db, a.id, actor, a.feedback)
    elif s == "reconcile":
        print(workers.reconcile(db) or "all workers consistent")
    elif s == "approve-spawn":
        ap = db.one("SELECT * FROM approvals WHERE id=? AND kind='spawn'", (a.approval,))
        if not ap or ap["status"] != "pending":
            raise MeshError("no such pending spawn approval")
        p = json.loads(ap["payload"])
        with db.tx() as c:
            c.execute("UPDATE approvals SET status=?, decided_by=?, decided=? WHERE id=?", ("rejected" if a.reject else "approved", actor, now(), a.approval))
        if a.reject:
            bus.send(db, actor, ap["requested_by"], "answer", f"spawn request #{a.approval} rejected", ap["subject"])
        else:
            w = workers.spawn(db, ap["subject"], p["agent"], actor=actor, depth=p["depth"], parent=p.get("parent"))
            bus.send(db, actor, ap["requested_by"], "answer", f"spawn request #{a.approval} approved: worker {w['id']}", ap["subject"])
            print(w["id"])
    return 0


def cmd_halt(db, a, actor):
    require_privileged(db, actor)
    ids = workers.halt(db, actor)
    print(f"halted; terminated {len(ids)} worker(s): {', '.join(ids) or '-'}. New spawns blocked until `mesh unhalt`.")
    return 0


def cmd_unhalt(db, a, actor):
    require_privileged(db, actor)
    workers.unhalt(db)
    return 0


AUTONOMY_TEXT = """\
Headless workers run with tool approvals PRE-GRANTED, because non-interactive runs cannot ask:
  claude       -p --permission-mode acceptEdits --allowedTools Bash,Read,Edit,Write,MultiEdit,Glob,Grep
  copilot      -p --allow-all-tools          (path/URL confinement stays ON)
  antigravity  -p --dangerously-skip-permissions
Mesh confines each worker to its own git worktree and guards it with hooks (destructive commands, secrets, locks,
protected files), spawn/cost/depth caps and `mesh halt`. Hooks prevent accidents; they are NOT a security sandbox.
Workers can still run any shell command inside their worktree. Only accept on a repo and machine where that is acceptable."""


def cmd_autonomy(db, a, actor):
    cfg = load_config(db.paths)
    if a.sub == "show":
        print(json.dumps(cfg["autonomy"], indent=2))
        return 0
    require_privileged(db, actor)
    print(AUTONOMY_TEXT)
    if not a.yes:
        if not sys.stdin.isatty():
            raise MeshError("not a terminal: re-run with --yes to record your acceptance explicitly")
        if input("\nType 'yes' to accept this autonomy policy for this project: ").strip().lower() != "yes":
            raise MeshError("not accepted")
    vals = {"mode": "worktree-confined", "accepted_by": getpass.getuser(), "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "accepted_cli_flags": "claude:acceptEdits; copilot:allow-all-tools; antigravity:dangerously-skip-permissions",
            "plan_approval": cfg["autonomy"].get("plan_approval", True)}
    core.set_toml_section(db.paths.config, "autonomy", vals)
    db.event(actor, "autonomy.accepted", None, vals)
    print("recorded in .mesh/mesh.toml [autonomy]")
    return 0


def cmd_commit_config(db, a, actor):
    root = db.paths.root
    cands = [".mesh", "AGENTS.md", "CLAUDE.md", ".claude", ".github/hooks", ".github/agents", ".agents/hooks.json", ".gitignore"]
    paths = [p for p in cands if (root / p).exists()]
    core.git(root, "add", "--", *paths)
    if core.git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        print("mesh config already committed")
        return 0
    core.git(root, "commit", "-m", "chore: add agent-mesh configuration", "--", *paths)
    print("committed mesh-generated files: " + ", ".join(paths))
    return 0


# ------------------------------------------------------------------ events

def cmd_wait(db, a, actor):
    workers.reconcile(db)
    rows = eventlog.wait(db, a.consumer, a.timeout, include_noisy=a.all, advance=not a.peek, actor=actor)
    if not rows:
        print(f"(no new events within {int(a.timeout)}s)")
        return 0
    print(eventlog.render(db, rows))
    return 0


def cmd_tail(db, a, actor):
    rows = eventlog.since(db, a.since, include_noisy=a.all, limit=a.limit)
    print(eventlog.render(db, rows) or "(no events)")
    return 0


# ------------------------------------------------------------------ verification gates

def cmd_submit(db, a, actor):
    print(verify.submit(db, a.task, actor, a.note))
    return 0


def cmd_verify(db, a, actor):
    require_privileged(db, actor)
    r = verify.verify(db, a.task, actor)
    print(("PASS" if r["ok"] else "FAIL") + (" (weak: no automated checks configured)" if r["weak"] else ""))
    print(r["detail"])
    return 0 if r["ok"] else 5


def cmd_approve(db, a, actor):
    require_privileged(db, actor)
    print(verify.approve(db, a.task, actor, accept_unverified=a.accept_unverified))
    if a.integrate:
        r = gitflow.integrate(db, a.task, actor)
        print(("OK: " if r["ok"] else f"FAILED at {r['stage']}: ") + r["detail"])
        return 0 if r["ok"] else 4
    return 0


def cmd_reject(db, a, actor):
    require_privileged(db, actor)
    print(verify.reject(db, a.task, actor, a.reason))
    return 0


def cmd_diff(db, a, actor):
    print(verify.diff(db, a.task))
    return 0


def cmd_review(db, a, actor):
    print(verify.review(db, a.task, actor, a.sub == "approve", a.note))
    return 0


def cmd_decide(db, a, actor):
    require_privileged(db, actor)
    m = db.one("SELECT * FROM messages WHERE id=?", (a.message,))
    if not m or m["type"] != "decision":
        raise MeshError("not a decision message")
    if a.verdict == "accept":
        name = memory.new_adr(db, f"Decision from {m['sender']}", f"Proposed by {m['sender']} on task {m['task_id']}", m["content"], actor, a.note)
        bus.send(db, actor, "*", "decision_result", f"RATIFIED {name}: {m['content'][:300]}", m["task_id"], 1, requires_ack=False, correlation_id=m["correlation_id"])
        print(name)
    else:
        bus.send(db, actor, m["sender"], "decision_result", f"REJECTED your proposal: {a.note or m['content'][:100]}", m["task_id"], 1,
                 requires_ack=True, correlation_id=m["correlation_id"], reply_to=m["id"])
    bus.ack(db, actor, a.message)
    return 0


def cmd_reply(db, a, actor):
    m = db.one("SELECT * FROM messages WHERE id=?", (a.message,))
    if not m:
        raise MeshError("unknown message")
    mid = bus.send(db, actor, m["sender"], "answer", a.text, m["task_id"], 1, requires_ack=True, correlation_id=m["correlation_id"], reply_to=m["id"])
    bus.ack(db, actor, a.message)
    print(mid)
    return 0


def cmd_dep(db, a, actor):
    tasks.add_dep(db, a.task, a.on)
    return 0


def cmd_plan(db, a, actor):
    cfg = load_config(db.paths)
    rows = db.q("SELECT * FROM tasks WHERE kind!='project' ORDER BY created")
    print("Task graph:")
    from .cli import cmd_task
    import argparse
    cmd_task(db, argparse.Namespace(sub="tree", id=None), actor)
    print("\nPlanned workers (routing from mesh.toml; first available CLI wins):")
    out = []
    for r in rows:
        if r["kind"] in ("epic", "project") or r["status"] in ("COMPLETED", "CANCELLED"):
            continue
        route = workers.route_for(db, cfg, r["agent"] or "backend")
        deps = [d[0] for d in db.q("SELECT dep_id FROM task_deps WHERE task_id=?", (r["id"],))]
        out.append([r["id"], r["title"][:34], r["agent"] or "backend", (route[0][0] if route else "NONE"), r["files"][:28], ",".join(deps) or "-", r["risk"] or "low"])
    print(_tbl(out, ["task", "title", "agent", "cli", "files", "deps", "risk"]))
    W = cfg["workers"]
    print(f"\nCaps: max_workers={W['max_workers']} max_depth={W['max_depth']} max_restarts={W['max_restarts']} "
          f"cost_cap=${W['max_total_cost_usd'] or 'none'} | autonomy accepted: {bool(cfg['autonomy']['accepted_at'])}")
    return 0


# ------------------------------------------------------------------ up / report

def _orchestrator_prompt(db: DB, cfg: dict, sid: str, user_prompt: str, resume: bool) -> str:
    pa = ("This project requires plan approval: present the plan with `mesh plan show` and WAIT for the user's go-ahead before spawning workers."
          if cfg["autonomy"].get("plan_approval", True) else "Plan approval is disabled: you may spawn workers right after planning.")
    base = playbooks.render(playbooks.ORCHESTRATOR, session=sid, plan_approval=pa)
    if resume:
        tail = ["\n## RESUMING an existing project (verify everything against the repository)", f"Open tasks:"]
        tail += [f"- {r['id']} [{r['status']}] {r['title']} (owner {r['session_id'] or '-'})" for r in db.q(
            "SELECT * FROM tasks WHERE status NOT IN ('COMPLETED','CANCELLED') AND kind!='project' ORDER BY created")]
        tail.append("Workers:")
        tail += [f"- {w['id']} {w['agent']} {w['cli']} {w['state']} task={w['task_id']}" for w in workers.list_workers(db)]
        pend = eventlog.render(db, eventlog.since(db, 0, limit=15))
        tail.append("Recent events:\n" + pend)
        return base + "\n".join(tail) + (f"\n\n## User request\n{user_prompt}" if user_prompt else "\nContinue managing the project from this state.")
    return base + f"\n## User request (project owner)\n{user_prompt}\n"


def cmd_up(db, a, actor):
    import subprocess
    cfg = load_config(db.paths)
    adapter = adapters.ADAPTERS[a.client](db.paths)
    if not adapter.available():
        raise MeshError(f"{a.client} is not installed / not on PATH")
    if not cfg["autonomy"]["accepted_at"]:
        cmd_autonomy(db, type("A", (), {"sub": "accept", "yes": a.accept_autonomy})(), "human")
    adapters.sync_all(db.paths, ["claude", "copilot", "antigravity"])
    atomic_write(db.paths.mesh / "instructions" / "ORCHESTRATOR.md", playbooks.ORCHESTRATOR.replace("{session}", "<session>").replace("{plan_approval}", ""))
    try:
        workers.check_committed(db, load_config(db.paths))
    except MeshError as e:
        if a.dry_run or not sys.stdin.isatty():
            raise
        print(f"{e}\nCommitting mesh-generated files now.")
        cmd_commit_config(db, a, actor)
    if not a.resume and not a.prompt:
        raise MeshError("give a project prompt: mesh up <client> \"Build ...\"   (or --resume)")
    sid = core.register_session(db, a.client, "orchestrator", None, os.getpid(), None, role="orchestrator", session_id=f"orch-{os.urandom(3).hex()}")
    prompt = _orchestrator_prompt(db, load_config(db.paths), sid, a.prompt, a.resume)
    cmd = adapter.launch_cmd(prompt)
    if a.dry_run:
        print(prompt)
        return 0
    env = {**os.environ, "MESH_SESSION": sid, "MESH_ROLE": "orchestrator", "MESH_ROOT": str(db.paths.root)}
    print(f"orchestrator session {sid} starting in {a.client}. Watch it with `mesh dash` in another terminal.", file=sys.stderr)
    rc = subprocess.call(cmd, env=env, cwd=db.paths.root)
    core.end_session(db, sid, "ended")
    live = workers.list_workers(db, active_only=True)
    if live:
        print(f"{len(live)} worker(s) still running. Re-attach with `mesh up {a.client} --resume`, stop them with `mesh halt`.", file=sys.stderr)
    return rc


def cmd_report(db, a, actor):
    from . import snapshot
    snap = snapshot.build(db.paths)
    print(snapshot.render_report(snap))
    return 0


HANDLERS = {n[4:]: f for n, f in list(globals().items()) if n.startswith("cmd_")}
