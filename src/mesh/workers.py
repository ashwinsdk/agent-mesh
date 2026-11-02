"""Worker lifecycle. Distinguish: agent (role definition), session (resumable CLI conversation), process (OS pid).

A worker row binds an agent role + a mesh session + the current runner process + its task assignment.
All state transitions go through the DB, so concurrent callers (orchestrator, user, reconcile) are safe.
"""
from __future__ import annotations

import fnmatch
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import adapters, bus, checkpoint, core, gitflow, memory, playbooks, tasks, usage
from .core import load_config
from .db import DB, MeshError, new_id, now

STATES = ["IDLE", "INITIALISING", "PLANNING", "WORKING", "COMMUNICATING", "REVIEWING", "BLOCKED", "PAUSED",
          "FAILED", "COMPLETED", "TERMINATED"]
TERMINAL = ("FAILED", "COMPLETED", "TERMINATED")
_children: dict[int, subprocess.Popen] = {}  # reaped when we spawned them in-process


# ------------------------------------------------------------------ process helpers

def proc_start(pid: int | None) -> str | None:
    if not pid:
        return None
    r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip() or None


def pid_alive(pid: int | None, start: str | None = None) -> bool:
    """Alive means: exists, not a zombie, and (if given) the same process that was started (PID-reuse guard)."""
    if not pid:
        return False
    ch = _children.get(pid)
    if ch is not None:
        return ch.poll() is None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    if not st or st.startswith("Z"):
        return False
    return start is None or proc_start(pid) == start


# ------------------------------------------------------------------ policy

def check_autonomy(cfg: dict) -> None:
    a = cfg["autonomy"]
    if not a.get("accepted_at"):
        raise MeshError("autonomy policy not accepted: headless workers run with tool approvals pre-granted. "
                        "Review and accept once with `mesh autonomy accept` (or start via `mesh up`).")


def check_committed(db: DB, cfg: dict) -> None:
    """Worktrees branch from the base branch; guard hooks/instructions must be committed there or workers run unguarded."""
    if not cfg["workers"].get("require_committed_config", True):
        return
    root = db.paths.root
    base = gitflow.base_branch(db)
    need = [".mesh/bin/mesh", ".mesh/mesh.toml"]
    missing = [f for f in need if core.git(root, "cat-file", "-e", f"{base}:{f}", check=False).returncode != 0]
    if missing:
        raise MeshError(f"mesh config is not committed on '{base}' ({', '.join(missing)}), so worker worktrees would lack the "
                        "safety hooks. Run `mesh commit-config` (commits only mesh-generated files), then retry.")


def route_for(db: DB, cfg: dict, agent: str | None) -> list[tuple[str, str | None]]:
    raw = cfg["workers"]["route"].get(agent or "") or cfg["workers"]["default_route"]
    out = []
    for item in raw:
        cli, _, model = item.partition(":")
        cls = adapters.ADAPTERS.get(cli)
        if cls and cli != "generic" and cls(db.paths).available():
            out.append((cli, model or None))
    return out


def active_count(db: DB) -> int:
    ph = ",".join("?" * len(TERMINAL))
    return db.one(f"SELECT COUNT(*) FROM workers WHERE state NOT IN ({ph})", TERMINAL)[0]


def spend_total(db: DB, cfg: dict) -> float:
    """Sum of latest cumulative cost per (worker, cli), counting only api-mode usage (real spend). Subscription usage is excluded."""
    return sum(u["cost_usd"] for u in usage.latest_series(db) if u["billing_mode"] == "api" and u["cost_usd"] is not None)


# ------------------------------------------------------------------ reads / state

def get(db: DB, wid: str):
    r = db.one("SELECT * FROM workers WHERE id=?", (wid,))
    if not r:
        raise MeshError(f"unknown worker {wid}")
    return r


def list_workers(db: DB, active_only: bool = False):
    rows = db.q("SELECT * FROM workers ORDER BY started")
    return [r for r in rows if not active_only or r["state"] not in TERMINAL]


def set_state(db: DB, wid: str, state: str, detail: str | None = None, **cols) -> None:
    assert state in STATES, state
    sets, args = ["state=?", "last_event=?"], [state, now()]
    if detail is not None:
        sets.append("state_detail=?"); args.append(detail)
    for k, v in cols.items():
        sets.append(f"{k}=?"); args.append(v)
    with db.tx() as c:
        old = c.execute("SELECT state FROM workers WHERE id=?", (wid,)).fetchone()
        c.execute(f"UPDATE workers SET {', '.join(sets)} WHERE id=?", (*args, wid))
        if old and old["state"] != state:
            db.event(wid, "worker.state", None, {"from": old["state"], "to": state, "detail": detail}, conn=c)


# ------------------------------------------------------------------ prompts

def decisions_text(db: DB, limit: int = 25) -> str:
    lines = []
    d = db.paths.memory / "decisions"
    for f in sorted(d.glob("ADR-*.md")) if d.exists() else []:
        txt = f.read_text()
        lines.append(f"- {f.stem}: " + txt.split("## Decision\n")[-1].split("\n## ")[0].strip().replace("\n", " ")[:300])
    proj, _ = memory.read(db, "project")
    lines += [l for l in proj.splitlines() if l.startswith("- [")][:limit]
    return "\n".join(lines) or "(none recorded yet; ask the orchestrator before making architectural choices)"


def build_prompt(db: DB, w, feedback: str | None = None) -> str:
    t = tasks.get(db, w["task_id"])
    base = playbooks.render(playbooks.WORKER, worker=w["id"], role=w["agent"] or "worker", session=w["id"],
                            worktree=w["worktree"], branch=t["branch"] or "", task_id=t["id"], title=t["title"],
                            description=t["description"] or "", acceptance=t["acceptance"] or "(see description)",
                            files=t["files"], verify=t["verify_cmd"] or "(project tests)", decisions=decisions_text(db))
    from . import verify
    up = verify.upstream_handoffs(db, t["id"])
    if up:
        base += f"\n\n## Completed upstream work you build on (already on branch mesh/integration)\n{up}\n"
    hist = (t["notes"] or "").strip()
    if hist:
        base += f"\n\n## History of this task (earlier attempts, rejections, merge problems; do not repeat them)\n{hist[-900:]}\n"
    if feedback:
        base += f"\n\n## Feedback from the orchestrator (address this first)\n{feedback}\n"
    return base


# ------------------------------------------------------------------ spawn

def _env_for_runner(db: DB) -> dict:
    env = dict(os.environ)
    env["MESH_ROOT"] = str(db.paths.root)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def start_runner(db: DB, wid: str) -> int:
    w = get(db, wid)
    Path(w["run_dir"]).mkdir(parents=True, exist_ok=True)
    log = open(Path(w["run_dir"]) / "runner.log", "ab")
    p = subprocess.Popen([sys.executable, "-m", "mesh", "worker", "run", wid], cwd=db.paths.root, env=_env_for_runner(db),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    _children[p.pid] = p
    with db.tx() as c:
        c.execute("UPDATE workers SET runner_pid=?, runner_start=?, pgid=?, last_event=? WHERE id=?",
                  (p.pid, proc_start(p.pid), p.pid, now(), wid))
        db.event(wid, "worker.runner_started", w["task_id"], {"pid": p.pid}, conn=c)
    return p.pid


def spawn(db: DB, task_id: str, agent: str | None = None, cli: str | None = None, model: str | None = None,
          actor: str = "human", depth: int = 0, parent: str | None = None, start: bool = True) -> dict:
    cfg = load_config(db.paths)
    check_autonomy(cfg)
    check_committed(db, cfg)
    if (db.one("SELECT value FROM meta WHERE key='halted'") or [None])[0] == "1":
        raise MeshError("spawning is halted (`mesh halt`); run `mesh unhalt` to allow new workers")
    W = cfg["workers"]
    if depth > W["max_depth"]:
        raise MeshError(f"delegation depth {depth} exceeds max_depth={W['max_depth']}")
    cap = W["max_total_cost_usd"]
    if cap and spend_total(db, cfg) >= cap:
        raise MeshError(f"total API spend cap reached (${cap}); raise [workers].max_total_cost_usd or finish up")
    t = tasks.get(db, task_id)
    agent = agent or t["agent"] or "backend"
    route = route_for(db, cfg, agent)
    if cli:
        if cli not in adapters.ADAPTERS or cli == "generic" or not adapters.ADAPTERS[cli](db.paths).available():
            raise MeshError(f"CLI '{cli}' is not available on this machine")
        idx = next((i for i, (c, _) in enumerate(route) if c == cli), 0)
        chosen = (cli, model or next((m for c, m in route if c == cli), None))
    elif route:
        idx, chosen = 0, (route[0][0], model or route[0][1])
    else:
        raise MeshError("no supported CLI is available for routing (check `mesh doctor`)")
    wid = new_id("w")
    t0 = now()
    with db.tx() as c:
        n = c.execute(f"SELECT COUNT(*) FROM workers WHERE state NOT IN ({','.join('?' * len(TERMINAL))})", TERMINAL).fetchone()[0]
        if n >= W["max_workers"]:
            raise MeshError(f"worker cap reached ({n}/{W['max_workers']}); wait for one to finish or terminate one")
        run_dir = str(db.paths.runtime / "runs" / wid)
        c.execute("INSERT INTO workers(id,agent,role,cli,model,depth,parent_worker,run_dir,task_id,started,route_idx,state) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (wid, agent, "worker", chosen[0], chosen[1], depth, parent, run_dir, task_id, t0, idx, "INITIALISING"))
        c.execute("INSERT INTO sessions(id,cli,agent,native_id,pid,host,model,started,last_seen,role,worker_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (wid, chosen[0], agent, None, None, os.uname().nodename, chosen[1], t0, t0, "worker", wid))
        db.event(actor, "worker.spawn", task_id, {"worker": wid, "agent": agent, "cli": chosen[0], "model": chosen[1]}, conn=c)
    try:
        tasks.claim(db, task_id, wid, agent, chosen[0], cfg["locks"]["ttl_seconds"])
        wt = gitflow.worktree_create(db, task_id)
    except BaseException:
        with db.tx() as c:
            c.execute("DELETE FROM workers WHERE id=?", (wid,))
            c.execute("DELETE FROM sessions WHERE id=?", (wid,))
        raise
    with db.tx() as c:
        c.execute("UPDATE workers SET worktree=? WHERE id=?", (str(wt), wid))
        c.execute("INSERT INTO assignments(worker_id,task_id,started) VALUES(?,?,?)", (wid, task_id, t0))
        c.execute("UPDATE sessions SET task_id=? WHERE id=?", (task_id, wid))
    w = get(db, wid)
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    (Path(run_dir) / "prompt.md").write_text(build_prompt(db, w))
    if start:
        start_runner(db, wid)
    return dict(get(db, wid))


# ------------------------------------------------------------------ control

def send(db: DB, wid: str, text: str, actor: str, type_: str = "info", priority: int = 1, requires_ack: bool = True) -> str | None:
    get(db, wid)
    return bus.send(db, actor, wid, type_, text, get(db, wid)["task_id"], priority, requires_ack=requires_ack)


def _kill_group(pgid: int | None, runner_pid: int | None, runner_start: str | None, grace: float = 6.0) -> None:
    if not pgid or not pid_alive(runner_pid, runner_start):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    end = time.time() + grace
    while time.time() < end and pid_alive(runner_pid, runner_start):
        time.sleep(0.1)
    if pid_alive(runner_pid, runner_start):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def terminate(db: DB, wid: str, actor: str, reason: str = "terminated by orchestrator") -> None:
    w = get(db, wid)
    if w["state"] in TERMINAL:
        return
    set_state(db, wid, "TERMINATED", reason, ended=now(), ended_reason=reason)  # first, so the runner will not restart
    _kill_group(w["pgid"], w["runner_pid"], w["runner_start"])
    _release_task(db, w, "terminated")
    core.end_session(db, wid, "terminated")
    db.event(actor, "worker.terminated", w["task_id"], {"worker": wid, "reason": reason})


def halt(db: DB, actor: str) -> list[str]:
    with db.tx() as c:
        c.execute("INSERT INTO meta(key,value) VALUES('halted','1') ON CONFLICT(key) DO UPDATE SET value='1'")
        db.event(actor, "halt", None, {}, conn=c)
    ids = []
    for w in list_workers(db, active_only=True):
        terminate(db, w["id"], actor, "halt")
        ids.append(w["id"])
    return ids


def unhalt(db: DB) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM meta WHERE key='halted'")


def _release_task(db: DB, w, outcome: str) -> None:
    try:
        t = tasks.get(db, w["task_id"])
        if t["status"] in ("ASSIGNED", "IN_PROGRESS") and t["session_id"] == w["id"]:
            tasks.reassign(db, w["task_id"], "mesh")
    except MeshError:
        pass
    with db.tx() as c:
        c.execute("UPDATE assignments SET ended=?, outcome=? WHERE worker_id=? AND ended IS NULL", (now(), outcome, w["id"]))


# ------------------------------------------------------------------ exit handling / recovery

def handle_exit(db: DB, wid: str, outcome: str, rc: int | None = None, detail: str = "") -> dict:
    """Decide what happens after a worker process ended. outcome: clean | limit | crash | timeout | stalled.
    Returns {'action': done|restart|fallback|failed|terminated|unsubmitted, ...}. The caller (runner loop or reconcile) acts on it."""
    cfg = load_config(db.paths)
    w = get(db, wid)
    if w["state"] == "TERMINATED":
        return {"action": "terminated"}
    t = tasks.get(db, w["task_id"])
    with db.tx() as c:
        c.execute("UPDATE workers SET exit_code=?, runner_pid=runner_pid WHERE id=?", (rc, wid))
    if outcome == "clean":
        if t["status"] in ("REVIEW", "COMPLETED"):
            set_state(db, wid, "COMPLETED", "submitted for review", ended=now(), ended_reason="submitted")
            _close_assignment(db, wid, "submitted")
            core.end_session(db, wid, "ended")
            db.event(wid, "worker.done", w["task_id"], {})
            return {"action": "done"}
        checkpoint.create(db, wid, w["task_id"], {"current_stage": "worker exited without submitting", "issues": [detail or "no submit"]}, reason="exit-unsubmitted")
        set_state(db, wid, "FAILED", "exited without submitting", ended=now(), ended_reason="exited_unsubmitted")
        _release_task(db, w, "unsubmitted")
        core.end_session(db, wid, "ended")
        db.event(wid, "worker.exited_unsubmitted", w["task_id"], {"worker": wid})
        bus.send(db, wid, "role:orchestrator", "blocker", f"worker {wid} exited without submitting task {w['task_id']} (agent {w['agent']}). {detail}",
                 w["task_id"], 1, requires_ack=True, dedupe_key=f"unsub-{wid}")
        return {"action": "unsubmitted"}
    # failure outcomes: save state first
    try:
        checkpoint.create(db, wid, w["task_id"], {"current_stage": f"worker run ended abnormally ({outcome})", "issues": [detail or outcome]}, reason=f"worker-{outcome}")
    except MeshError:
        pass
    W = cfg["workers"]
    if outcome == "limit":
        db.event(wid, "worker.limit", w["task_id"], {"cli": w["cli"], "detail": detail[:200]})
        usage.record_rate_limit(db, wid, w["cli"], detail or "limit")
        route = route_for(db, cfg, w["agent"])
        nxt = next(((i, c, m) for i, (c, m) in enumerate(route) if c != w["cli"] and i > w["route_idx"]), None) or \
            next(((i, c, m) for i, (c, m) in enumerate(route) if c != w["cli"]), None)
        if nxt and w["restarts"] < W["max_restarts"]:
            i, c, m = nxt
            with db.tx() as cx:
                cx.execute("UPDATE workers SET cli=?, model=?, route_idx=?, restarts=restarts+1, native_session=NULL, state_detail='fresh', "
                           "state='INITIALISING', last_event=? WHERE id=?", (c, m, i, now(), wid))
                cx.execute("UPDATE sessions SET cli=?, model=? WHERE id=?", (c, m, wid))
                db.event(wid, "worker.fallback", w["task_id"], {"from": w["cli"], "to": c}, conn=cx)
            return {"action": "fallback", "cli": c}
        return _fail(db, w, "usage limit reached and no fallback CLI available", "worker.failed")
    if w["restarts"] < W["max_restarts"]:
        resume = bool(w["native_session"]) and adapters.ADAPTERS[w["cli"]](db.paths).supports_resume
        with db.tx() as cx:
            cx.execute("UPDATE workers SET restarts=restarts+1, state='INITIALISING', state_detail=?, last_event=? WHERE id=?",
                       ("resume" if resume else "fresh", now(), wid))
            db.event(wid, "worker.restart", w["task_id"], {"outcome": outcome, "resume": resume}, conn=cx)
        return {"action": "restart", "resume": resume}
    return _fail(db, w, f"{outcome}; restart budget exhausted", "worker.failed")


def _fail(db: DB, w, why: str, ev: str) -> dict:
    set_state(db, w["id"], "FAILED", why, ended=now(), ended_reason=why)
    _release_task(db, w, "failed")
    core.end_session(db, w["id"], "failed")
    db.event(w["id"], ev, w["task_id"], {"worker": w["id"], "why": why})
    bus.send(db, w["id"], "role:orchestrator", "blocker", f"worker {w['id']} failed on task {w['task_id']}: {why}. Task returned to READY.",
             w["task_id"], 1, requires_ack=True, dedupe_key=f"fail-{w['id']}")
    return {"action": "failed", "why": why}


def _close_assignment(db: DB, wid: str, outcome: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE assignments SET ended=?, outcome=? WHERE worker_id=? AND ended IS NULL", (now(), outcome, wid))


def reconcile(db: DB) -> list[str]:
    """Detect workers whose runner died (kill -9, reboot, crash) and apply the exit policy. Safe to call anytime, from anywhere."""
    acted = []
    for w in list_workers(db, active_only=True):
        if pid_alive(w["runner_pid"], w["runner_start"]):
            continue
        if w["runner_pid"] is None and now() - w["started"] < 15:
            continue  # still being set up
        db.event("mesh", "worker.runner_dead", w["task_id"], {"worker": w["id"], "pid": w["runner_pid"]})
        res = handle_exit(db, w["id"], "crash", None, "runner process disappeared")
        if res["action"] in ("restart", "fallback"):
            start_runner(db, w["id"])
        acted.append(f"{w['id']}:{res['action']}")
    return acted


def restart(db: DB, wid: str, actor: str, feedback: str | None = None) -> None:
    w = get(db, wid)
    if pid_alive(w["runner_pid"], w["runner_start"]):
        raise MeshError(f"worker {wid} is still running; terminate it first or `mesh worker send`")
    t = tasks.get(db, w["task_id"])
    if w["state"] in ("FAILED", "COMPLETED") or t["session_id"] != wid:
        # re-claim ownership for this worker
        if t["session_id"] not in (None, wid):
            raise MeshError(f"task {t['id']} is now owned by {t['session_id']}")
        if t["status"] in ("COMPLETED",):
            raise MeshError("task already completed")
        if t["status"] == "READY":
            tasks.claim(db, t["id"], wid, w["agent"], w["cli"])
    if feedback:
        with db.tx() as c:
            c.execute("INSERT INTO messages(id,sender,recipient,ts,task_id,type,priority,content,requires_ack) VALUES(?,?,?,?,?,?,?,?,1)",
                      (new_id("M"), actor, wid, now(), w["task_id"], "review", 1, feedback))
    with db.tx() as c:
        c.execute("UPDATE workers SET state='INITIALISING', state_detail=?, ended=NULL, ended_reason=NULL, last_event=? WHERE id=?",
                  ("resume" if w["native_session"] else "fresh", now(), wid))
        c.execute("UPDATE sessions SET status='active', ended=NULL WHERE id=?", (wid,))
        db.event(actor, "worker.restart_requested", w["task_id"], {"worker": wid}, conn=c)
    start_runner(db, wid)
