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
    x.add_argument("client", nargs="?", default=None, choices=["claude", "copilot", "antigravity"]); x.add_argument("prompt", nargs="?", default="")
    x.add_argument("--resume", action="store_true"); x.add_argument("--accept-autonomy", action="store_true")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--legacy", action="store_true", help="old behaviour: start an interactive LLM orchestrator session (deprecated)")
    sub.add_parser("report")
    x = sub.add_parser("conduct", help="run the conductor (single instance per project)"); x.add_argument("--daemon", action="store_true")
    x.add_argument("--status", action="store_true"); x.add_argument("--stop", action="store_true")
    x = sub.add_parser("start", help="headless: submit a project prompt to the conductor"); x.add_argument("prompt")
    x.add_argument("--cli"); x.add_argument("--yes", action="store_true", help="accept the autonomy policy non-interactively")
    x.add_argument("--wait", action="store_true", help="block until the run reaches a terminal state or needs you")
    sub.add_parser("status", help="plain-text project status")
    x = sub.add_parser("ctl", help="send a command to the conductor (scripting)"); x.add_argument("kind"); x.add_argument("payload", nargs="?", default="{}")
    x = sub.add_parser("probe", help="run ONE tiny headless turn in a real CLI and report how well mesh's stream parser understood it")
    x.add_argument("client", choices=["claude", "copilot", "antigravity"]); x.add_argument("--out", default="probe-output")
    x.add_argument("--yes", action="store_true", help="confirm: this runs the real CLI with tool approvals pre-granted")
    x.add_argument("--hooks", action="store_true", help="also verify that a headless run fires mesh hooks and receives an injected mailbox message")
    x.add_argument("--planner", action="store_true", help="instead verify the planner path: one structured-output planning turn, validated like a real plan")
    x.add_argument("--prompt", default="Create a file named hello.txt containing the word hi using your file tool, then reply with the single word done.")


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
    if not a.legacy:
        # v3: `mesh up` is just the app. The first positional may be a CLI name (old habit) or the prompt itself.
        from . import cli as _cli
        prompt = a.prompt or ""
        if a.client is None:
            pass
        db.close()
        print("note: `mesh up` now opens the app; the conductor runs the project. (Old behaviour: --legacy)", file=sys.stderr)
        return _cli.open_app(prompt or None)
    if a.client is None:
        raise MeshError("--legacy needs a client: mesh up claude --legacy \"prompt\"")
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


def cmd_conduct(db, a, actor):
    from . import conductor
    if a.status:
        print(json.dumps(conductor.status(db.paths), indent=2, default=str)); return 0
    if a.stop:
        print("stopping" if conductor.stop_daemon(db.paths) else "no conductor running"); return 0
    if a.daemon:
        print(f"conductor pid {conductor.start_daemon(db.paths)}"); return 0
    db.close()
    return conductor.serve(db.paths)


def cmd_start(db, a, actor):
    from . import conductor, runs
    cfg = load_config(db.paths)
    if not cfg["autonomy"]["accepted_at"]:
        cmd_autonomy(db, type("A", (), {"sub": "accept", "yes": a.yes})(), "human")
    adapters.sync_all(db.paths, ["claude", "copilot", "antigravity"])
    try:
        workers.check_committed(db, load_config(db.paths))
    except MeshError as e:
        print(f"{e}\nCommitting mesh-generated files now.")
        cmd_commit_config(db, a, actor)
    conductor.start_daemon(db.paths)
    cid = runs.enqueue(db, "submit_prompt", {"prompt": a.prompt, "cli": a.cli}, "human")
    for _ in range(100):
        r = db.one("SELECT * FROM commands WHERE id=?", (cid,))
        if r["status"] != "pending":
            print(("started: " if r["status"] == "done" else "error: ") + (r["result"] or ""))
            if r["status"] != "done":
                return 1
            break
        time.sleep(0.2)
    else:
        print("the conductor did not pick up the command (see .mesh/runtime/logs/conductor.log)"); return 1
    while a.wait:
        run = runs.latest_run(db)
        open_items = runs.open_attention(db)
        if run["status"] in runs.TERMINAL or run["status"] == "awaiting_approval" or open_items:
            print(f"run {run['id']}: {run['status']}")
            for i in open_items:
                print(f"  needs you: [{i['kind']}] {i['title']}")
            break
        time.sleep(2)
    return 0


def cmd_status(db, a, actor):
    from . import conductor, health, runs, snapshot
    s = snapshot.build(db.paths)
    st = conductor.status(db.paths)
    run = runs.latest_run(db)
    print(f"project {s['project']['name']}   conductor: {'running' if st['running'] else 'stopped'}   run: " +
          (f"#{run['id']} {run['status']} — {run['prompt'][:70]}" if run else "none"))
    print(snapshot.render_report(s))
    for i in runs.open_attention(db):
        print(f"NEEDS YOU [{i['kind']}] {i['title']}")
    for lvl, txt in health.checks(db):
        if lvl != "ok":
            print(f"{lvl.upper()}: {txt}")
    return 0


def cmd_ctl(db, a, actor):
    from . import conductor, runs
    cid = runs.enqueue(db, a.kind, json.loads(a.payload), actor)
    conductor.start_daemon(db.paths)
    print(cid)
    return 0


def cmd_probe(db, a, actor):
    """Live capture for parser verification (user-initiated; spends a few tokens). Never used by orchestration itself."""
    import subprocess, tempfile, collections
    from . import streams
    if not a.yes:
        print(AUTONOMY_TEXT + f"\n\nprobe runs `{a.client}` headless once in a scratch copy of this directory's git state. Re-run with --yes to proceed.")
        return 1
    cfg = load_config(db.paths)
    ad = adapters.ADAPTERS[a.client](db.paths)
    if not ad.available():
        raise MeshError(f"{a.client} not installed")
    out = Path(a.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    if a.planner:
        from . import brain, registry as _reg
        run = {"prompt": "Create a tiny static website with a home page, an about page and one shared stylesheet.", "id": 0}
        prompt = brain.planner_prompt(db, run)
        t0 = time.time()
        res = brain.call(db, a.client, prompt, brain.PLAN_SCHEMA, None, 900, "probe-plan")
        src = db.paths.runtime / "runs" / "probe-plan"
        for f in ("last.stdout.jsonl", "last.stderr.log"):
            if (src / f).exists():
                (out / f"{a.client}.planner.{f}").write_text((src / f).read_text())
        errs = brain.validate_plan(res.obj, {x["id"] for x in _reg.load_agents(db.paths.agents)}) if res.obj else ["no JSON object found in the answer"]
        print(f"planner turn finished in {time.time() - t0:.0f}s   error: {res.error or 'none'}   session id: {res.session or 'NO'}")
        print(f"structured/JSON plan extracted: {'YES' if res.obj else 'NO'}   plan valid: {'YES' if res.obj and not errs else 'NO'}")
        if res.obj and not errs:
            print("plan summary: " + brain.plan_summary(res.obj))
        else:
            print("validation problems: " + "; ".join(errs[:6]))
            print(f"answer text starts: {res.text[:200]!r}")
        print(f"usage: {res.usage}   cost: {res.cost}   AIU: {res.aiu}")
        print(f"raw output saved in {out} (send {a.client}.planner.last.stdout.jsonl if anything is NO)")
        return 0
    scratch = Path(tempfile.mkdtemp(prefix=f"mesh-probe-{a.client}-"))
    core.git(scratch, "init", "-q", "-b", "main"); (scratch / "README.md").write_text("probe\n")
    core.git(scratch, "add", "-A"); core.git(scratch, "-c", "user.email=p@p", "-c", "user.name=probe", "commit", "-qm", "probe")
    prompt = a.prompt
    token = "PROBE-TOKEN-7731"
    if a.hooks:
        # a mesh-initialised scratch project with a registered worker-like session and one pending message for it
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        henv = {**os.environ, "MESH_ROOT": str(scratch), "PYTHONPATH": pkg_parent}
        r0 = subprocess.run([sys.executable, "-m", "mesh", "init"], cwd=scratch, env=henv, capture_output=True, text=True)
        if r0.returncode != 0:
            raise MeshError("could not initialise scratch mesh project: " + r0.stderr[-300:])
        core.git(scratch, "add", "-A"); core.git(scratch, "-c", "user.email=p@p", "-c", "user.name=probe", "commit", "-qm", "mesh")
        from .db import DB as _DB, Paths as _Paths
        sdb = _DB(_Paths(scratch))
        core.register_session(sdb, a.client, "probe", None, os.getpid(), None, role="worker", session_id="w-probe")
        bus.send(sdb, "human", "w-probe", "info", f"the magic word is {token}", None, 1, requires_ack=True)
        sdb.close()
        prompt = ("First run the shell command `ls` with your shell tool. Then create a file named hello.txt using your file tool. "
                  "If any message from mesh (inside <mesh-messages> tags) appeared in your context, write its magic word into hello.txt; "
                  "otherwise write the word none. Reply with the single word done.")
    cmd, extra = ad.headless_cmd(prompt, None, None, None, cfg, str(out))
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LANG", "TERM", "TMPDIR", "SHELL")}
    if a.hooks:
        env.update({"MESH_ROOT": str(scratch), "MESH_SESSION": "w-probe", "MESH_ROLE": "worker", "MESH_WORKER": "w-probe"})
    env.update({k: v for k, v in os.environ.items() if k.endswith("_API_KEY") or k.startswith(("COPILOT_", "CLAUDE_CODE_", "GITHUB_TOKEN", "FAKE_"))})
    env.update(extra)
    print(f"running in {scratch}:\n  " + " ".join(cmd[:3]) + " ... " + " ".join(cmd[4:]))
    r = subprocess.run(cmd, cwd=scratch, env=env, capture_output=True, text=True, timeout=600)
    (out / f"{a.client}.stream.jsonl").write_text(r.stdout); (out / f"{a.client}.stderr.log").write_text(r.stderr)
    kinds = collections.Counter(); sess = None; usage_seen = None; cost = None; result = None; aiu_seen = None
    for line in r.stdout.splitlines():
        for e in streams.parse_line(a.client, line):
            kinds[e["kind"]] += 1
            sess = sess or e.get("session")
            if e["kind"] == "usage":
                if e.get("input") is not None or e.get("output") is not None:
                    usage_seen = e
                if e.get("nano_aiu") is not None:
                    aiu_seen = e["nano_aiu"] / 1e9
            if e["kind"] == "result":
                result, cost = e, e.get("cost")
    otel = out / "otel.jsonl"
    otel_usage = streams.parse_otel_file(otel.read_text(errors="ignore")) if otel.exists() else None
    print(f"exit code: {r.returncode}   hello.txt created: {(scratch / 'hello.txt').exists()}")
    print(f"parsed event kinds: {dict(kinds)}")
    print(f"native session id found: {sess or 'NO'}   token usage in stream: {'yes' if usage_seen else 'NO'}   cost in stream: {cost if cost is not None else 'NO'}   "
          f"AI credits in stream: {('%.4f AIU' % aiu_seen) if aiu_seen is not None else 'NO'}   "
          f"result event: {'yes ok=' + str(result.get('ok')) if result else 'NO'}")
    if a.client == "copilot":
        print(f"OTel token usage: {otel_usage or 'NOT FOUND (file ' + ('exists' if otel.exists() else 'missing') + ')'}")
    if a.hooks:
        from .db import DB as _DB, Paths as _Paths
        sdb = _DB(_Paths(scratch))
        tool_ev = sdb.one("SELECT COUNT(*) FROM events WHERE type='tool' AND actor='w-probe'")[0]
        sess_n = sdb.one("SELECT COUNT(*) FROM sessions")[0]
        msg = sdb.one("SELECT delivered_at, delivery_state FROM messages WHERE recipient='w-probe'")
        errlog = _Paths(scratch).logs / "hook-errors.log"
        content = (scratch / "hello.txt").read_text() if (scratch / "hello.txt").exists() else ""
        sdb.close()
        print("--- hook verification (headless mode) ---")
        print(f"hooks fired (tool events recorded by PostToolUse/pre_invocation hook): {'YES (%d)' % tool_ev if tool_ev else 'NO  -> hooks are NOT running in headless mode; workers would be unguarded'}")
        print(f"mailbox message marked delivered by the hook: {'YES' if msg and msg['delivered_at'] else 'NO'}")
        print(f"agent actually saw the injected message (token written to hello.txt): {'YES' if token in content else 'NO (hello.txt: %r)' % content[:60]}")
        print(f"hook errors logged: {errlog.read_text()[-300:] if errlog.exists() else 'none'}")
    raw = kinds.get("other", 0)
    print(f"unparsed lines: {raw} (high numbers mean the parser does not match this CLI's real schema; send {out}/{a.client}.stream.jsonl to the maintainer)")
    print(f"raw output saved in {out}")
    return 0


HANDLERS = {n[4:]: f for n, f in list(globals().items()) if n.startswith("cmd_")}
