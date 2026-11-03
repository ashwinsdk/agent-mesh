"""The conductor: a deterministic, single-instance scheduler that drives a run from prompt to completion.

Nothing here depends on an LLM remembering to act. LLMs are used for (1) the one-shot plan and (2) occasional judgement
turns whose output is validated JSON that this code executes. Everything else (dispatch, verify, approve, integrate,
restart, escalate) is plain code. The TUI/CLI never mutate scheduling state: they enqueue rows in `commands`.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, adapters, brain, bus, core, eventlog, gitflow, limits, memory, runs, tasks, verify, workers
from .core import load_config
from .db import DB, MeshError, Paths, now

LEAF_SKIP = ("epic", "project")
BRAIN_TYPES = ("question", "blocker", "decision", "spawn_request")


# ------------------------------------------------------------------ lock / status / daemon

def _lock_path(paths: Paths) -> Path:
    return paths.runtime / "conductor.lock"


def acquire_lock(paths: Paths):
    """Exclusive non-blocking flock; returns the open file (hold it for the process lifetime) or None if another conductor runs."""
    paths.runtime.mkdir(parents=True, exist_ok=True)
    f = open(_lock_path(paths), "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def status(paths: Paths) -> dict:
    try:
        db = DB(paths, busy_ms=1500)
    except Exception:
        return {"running": False, "pid": None, "heartbeat_age": None, "state": None}
    try:
        r = db.one("SELECT * FROM conductor WHERE id=1")
    finally:
        db.close()
    if not r or not r["pid"]:
        return {"running": False, "pid": None, "heartbeat_age": None, "state": None}
    alive = workers.pid_alive(r["pid"], r["pid_start"])
    age = now() - (r["heartbeat"] or 0)
    return {"running": bool(alive and age < 20), "pid": r["pid"], "heartbeat_age": age, "state": r["state"], "version": r["version"], "run": r["run_id"]}


def start_daemon(paths: Paths) -> int | None:
    """Start a detached conductor unless one is already running. Returns its pid (or the existing pid)."""
    st = status(paths)
    if st["running"]:
        return st["pid"]
    env = dict(os.environ)
    env["MESH_ROOT"] = str(paths.root)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    paths.logs.mkdir(parents=True, exist_ok=True)
    log = open(paths.logs / "conductor.log", "ab")
    p = subprocess.Popen([sys.executable, "-m", "mesh", "conduct"], cwd=paths.root, env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    workers._children[p.pid] = p
    for _ in range(60):  # wait briefly for the lock + heartbeat row so callers can rely on status()
        time.sleep(0.1)
        if status(paths)["running"]:
            break
    return p.pid


def stop_daemon(paths: Paths) -> bool:
    st = status(paths)
    if not st["pid"]:
        return False
    try:
        os.kill(st["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


# ------------------------------------------------------------------ jobs run in worker threads (own DB connections, return plain data)

def _plan_job(paths: Paths, run_id: int, cli: str, feedback: str | None, timeout: float) -> dict:
    db = DB(paths)
    try:
        run = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        agent_ids = {a["id"] for a in registry_agents(db)}
        prompt = brain.planner_prompt(db, run, feedback)
        res = brain.call(db, cli, prompt, brain.PLAN_SCHEMA, None, timeout, f"plan-{run_id}")
        brain.record_usage(db, run_id, cli, res)
        errs = []
        if res.obj:
            errs = brain.validate_plan(res.obj, agent_ids)
            if errs and not res.limit:  # one automatic repair attempt with the validator's complaints
                fix = prompt + "\n\n## Your previous answer was INVALID\n" + "\n".join(f"- {e}" for e in errs) + "\nReturn the corrected, complete JSON object."
                res2 = brain.call(db, cli, fix, brain.PLAN_SCHEMA, None, timeout, f"plan-{run_id}")
                brain.record_usage(db, run_id, cli, res2)
                if res2.obj:
                    errs2 = brain.validate_plan(res2.obj, agent_ids)
                    res, errs = res2, errs2
        return {"plan": res.obj if (res.obj and not errs) else None, "errors": errs, "error": res.error, "limit": res.limit,
                "text": res.text[:400], "raw": res.obj}
    finally:
        db.close()


def _brain_job(paths: Paths, run_id: int, cli: str, prompt: str, resume: str | None, timeout: float) -> dict:
    db = DB(paths)
    try:
        res = brain.call(db, cli, prompt, brain.ACTION_SCHEMA, resume, timeout, f"brain-{run_id}")
        brain.record_usage(db, run_id, cli, res)
        return {"obj": res.obj, "error": res.error, "limit": res.limit, "session": res.session, "text": res.text[:300]}
    finally:
        db.close()


def _verify_job(paths: Paths, task_id: str) -> dict:
    db = DB(paths)
    try:
        return verify.verify(db, task_id, "conductor")
    finally:
        db.close()


def _limits_job(paths: Paths, cli: str) -> dict:
    db = DB(paths)
    try:
        r = limits.refresh(db, cli)
        return {"cli": cli, "ok": r["ok"], "windows": len(r["windows"]), "error": r["error"]}
    finally:
        db.close()


def _final_job(paths: Paths, run_id: int) -> dict:
    db = DB(paths)
    try:
        cfg = load_config(paths)
        iw = paths.worktrees / "_integration"
        cwd = iw if iw.exists() else paths.root
        ok, out = gitflow.run_validation(cwd, cfg)
        return {"ok": ok, "out": out}
    finally:
        db.close()


def registry_agents(db: DB):
    from . import registry
    return registry.load_agents(db.paths.agents)


# ------------------------------------------------------------------ conductor

class Conductor:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.db = DB(paths)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mesh-conductor")
        self.futs: dict[tuple, tuple] = {}
        self.stop_flag = False
        self.handled: set[tuple] = set()
        self.brain_backoff_until = 0.0
        self.last_limits = 0.0
        self.last_brain_end = 0.0
        self.started = now()
        self._idle_since = now()

    def limits_refresh(self, force: bool = False, copilot: bool = False) -> str:
        """Ask Antigravity for its own /usage (free: zero tokens, verified) in a background thread. The first explicit refresh opts the project in
        to periodic ones. Copilot's `-p /usage` runs a real model session (~0.7 AIU, verified), so it only ever runs on explicit request."""
        if force and not self.cfg()["limits"]["enabled"]:
            core.set_toml_section(self.paths.config, "limits", {"enabled": True, "refresh_seconds": self.cfg()["limits"]["refresh_seconds"]})
        self.last_limits = now()
        started = []
        for cli in ("antigravity",) + (("copilot",) if copilot else ()):
            if adapters.ADAPTERS[cli](self.paths).available() and ("limits", cli) not in self.futs:
                self.futs[("limits", cli)] = (self.pool.submit(_limits_job, self.paths, cli), {})
                started.append(cli)
        return "asked " + ", ".join(started) + " for usage limits" if started else "no CLI to ask (antigravity not installed)"

    # -- plumbing
    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        with self.db.tx() as c:
            c.execute("UPDATE conductor SET pid=NULL, state='stopped', heartbeat=? WHERE id=1", (now(),))
        self.db.close()

    def heartbeat(self, run):
        with self.db.tx() as c:
            c.execute("INSERT INTO conductor(id,pid,pid_start,started,heartbeat,state,run_id,version) VALUES(1,?,?,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET pid=excluded.pid, pid_start=excluded.pid_start, heartbeat=excluded.heartbeat, state=excluded.state, run_id=excluded.run_id, version=excluded.version",
                      (os.getpid(), workers.proc_start(os.getpid()), self.started, now(), run["status"] if run else "idle", run["id"] if run else None, __version__))
            c.execute("UPDATE sessions SET last_seen=? WHERE role='orchestrator' AND status='active'", (now(),))
            busy = any(k[0] in ("brain", "plan") for k in self.futs)
            c.execute("INSERT INTO meta(key,value) VALUES('brain_busy',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if busy else "0",))

    def cfg(self) -> dict:
        return load_config(self.paths)

    # -- one scheduling pass
    def tick(self) -> list[str]:
        db, acts = self.db, []
        cfg = self.cfg()
        workers.reconcile(db)
        bus.sweep(db)
        core.mark_stale(db, cfg["sessions"]["stale_after"])
        acts += self.process_commands()
        self.collect_limits()
        if cfg["limits"]["enabled"] and now() - self.last_limits >= cfg["limits"]["refresh_seconds"]:
            self.limits_refresh()
        run = runs.active_run(db)
        self.heartbeat(run)
        if not run:
            return acts
        acts += self.collect(run)
        run = runs.active_run(db)
        if not run:
            return acts
        st = run["status"]
        if st == "planning":
            acts += self.start_planner(run)
        elif st == "executing":
            acts += self.dispatch(run, cfg)
            acts += self.review(run, cfg)
            acts += self.maybe_brain(run, cfg)
            acts += self.completion(run)
        elif st == "integrating":
            acts += self.start_final(run)
        return acts

    # -- commands (the only way the UI changes scheduling state)
    def process_commands(self) -> list[str]:
        acts = []
        for c in runs.pending_commands(self.db):
            try:
                msg = self.handle_command(c["kind"], json.loads(c["payload"] or "{}"), c["actor"] or "human")
                runs.finish_command(self.db, c["id"], True, msg or "")
                acts.append(f"cmd:{c['kind']}")
            except Exception as e:  # a bad command must never kill the conductor
                runs.finish_command(self.db, c["id"], False, str(e))
                self.db.event("conductor", "command.error", None, {"kind": c["kind"], "error": str(e)[:200]})
        return acts

    def handle_command(self, kind: str, p: dict, actor: str) -> str:
        db = self.db
        run = runs.active_run(db) or runs.latest_run(db)
        cfg = self.cfg()
        if kind == "submit_prompt":
            text = (p.get("prompt") or "").strip()
            if not text:
                raise MeshError("empty prompt")
            act = runs.active_run(db)
            if act:  # a message during a run is a directive to the orchestrator brain
                bus.send(db, "human", "role:orchestrator", "info", text, None, 1, requires_ack=True)
                return "sent to the orchestrator"
            cli = p.get("cli") or cfg["app"].get("cli") or next((n for n in ("claude", "copilot", "antigravity") if adapters.ADAPTERS[n](db.paths).available()), None)
            if not cli:
                raise MeshError("no supported CLI found on this machine")
            if not cfg["app"].get("cli"):
                core.set_toml_section(self.paths.config, "app", {"cli": cli})  # remember it so the app does not ask again
            rid = runs.create_run(db, text, cli, actor)
            core.register_session(db, cli, "orchestrator", None, os.getpid(), None, role="orchestrator", session_id=f"orch-{rid}")
            return f"run {rid} started with {cli}"
        if kind == "stop":
            self.stop_flag = True
            return "stopping"
        if kind == "kill_session":
            return core.kill_session(db, p["id"], actor)
        if kind == "clear_session":
            n = core.clear_sessions(db, p.get("scope") or p.get("id"), actor)
            return f"cleared {n} session(s)"
        if kind == "refresh_limits":
            return self.limits_refresh(force=True, copilot=bool(p.get("copilot")))
        if kind == "promote":
            return gitflow.promote(db) or "promoted"
        if kind in ("approve_item", "reject_item"):
            return self.decide_item(int(p["id"]), kind == "approve_item", actor, p.get("note", ""))
        if not run:
            raise MeshError("there is no run yet; type a project prompt first")
        rid = run["id"]
        if kind == "approve_plan":
            if run["status"] != "awaiting_approval":
                raise MeshError(f"run is {run['status']}, not awaiting approval")
            runs.set_status(db, rid, "executing", "plan approved")
            for a in db.q("SELECT id FROM attention WHERE status='open' AND kind='plan_approval' AND run_id=?", (rid,)):
                runs.resolve_attention(db, a["id"], actor)
            return "plan approved; dispatching"
        if kind in ("reject_plan", "replan"):
            for t in db.q("SELECT id,status FROM tasks WHERE run_id=? AND status NOT IN ('COMPLETED','CANCELLED')", (rid,)):
                try:
                    tasks.transition(db, t["id"], "CANCELLED", actor, "plan replaced", force=True)
                except MeshError:
                    pass
            with db.tx() as c:
                c.execute("UPDATE runs SET plan_attempts=0, plan_json=NULL, error=NULL WHERE id=?", (rid,))
                c.execute("UPDATE attention SET status='resolved', resolved_by=? WHERE status='open' AND run_id=? AND kind IN ('plan_approval','error')", (actor, rid))
                c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"plan_feedback_{rid}", p.get("feedback", "")))
            runs.set_status(db, rid, "planning", "replanning")
            return "replanning"
        if kind == "pause":
            runs.set_status(db, rid, "paused", "paused by user"); return "paused (running workers continue; nothing new is dispatched or approved)"
        if kind == "resume":
            if run["status"] == "halted":
                workers.unhalt(db)
            runs.set_status(db, rid, "executing", "resumed"); return "resumed"
        if kind == "halt":
            ids = workers.halt(db, actor)
            runs.set_status(db, rid, "halted", "halted by user")
            return f"halted; terminated {len(ids)} worker(s)"
        if kind == "set_cli":
            if p["cli"] not in adapters.ADAPTERS or p["cli"] == "generic":
                raise MeshError("unknown CLI")
            with db.tx() as c:
                c.execute("UPDATE runs SET cli=?, brain_session=NULL WHERE id=?", (p["cli"], rid))
            return f"orchestrator CLI is now {p['cli']}"
        if kind == "retry_task":
            tid = p["task"]
            t = tasks.get(db, tid)
            w = db.one("SELECT id FROM workers WHERE task_id=? AND state NOT IN ('FAILED','COMPLETED','TERMINATED')", (tid,))
            if w:
                workers.terminate(db, w["id"], actor, "retry requested")
            if tasks.get(db, tid)["status"] not in ("READY", "COMPLETED"):
                tasks.transition(db, tid, "READY", actor, "retry requested", force=True)
            for a in db.q("SELECT id FROM attention WHERE status='open' AND subject=?", (tid,)):
                runs.resolve_attention(db, a["id"], actor)
            self.handled = {h for h in self.handled if h[0] != tid}
            return f"task {tid} re-queued"
        if kind == "kill_worker":
            workers.terminate(db, p["worker"], actor, "killed by user"); return f"terminated {p['worker']}"
        if kind == "say":
            workers.send(db, p["worker"], p["text"], actor); return "message queued for delivery"
        raise MeshError(f"unknown command {kind}")

    def decide_item(self, aid: int, approve: bool, actor: str, note: str) -> str:
        db = self.db
        a = db.one("SELECT * FROM attention WHERE id=?", (aid,))
        if not a or a["status"] != "open":
            raise MeshError("that item is no longer open")
        if a["kind"] == "plan_approval":
            return self.handle_command("approve_plan" if approve else "reject_plan", {"feedback": note}, actor)
        if a["kind"] == "task_review":
            tid = a["subject"]
            if approve:
                t = tasks.get(db, tid)
                if t["review_required"] or t["risk"] == "high":
                    verify.review(db, tid, "human", True, note or "approved by the user")
                v = verify.latest_verification(db, tid)
                if not v or (v["sha"] != verify.head_sha(db, t)):
                    verify.verify(db, tid, actor)
                verify.approve(db, tid, actor, accept_unverified=True)
                r = gitflow.integrate(db, tid, actor)
                runs.resolve_attention(db, aid, actor)
                return "approved and merged" if r["ok"] else f"approved; merge needs rework: {r['detail'][:120]}"
            verify.reject(db, tid, actor, note or "rejected by the user")
            runs.resolve_attention(db, aid, actor)
            return "sent back for rework"
        if a["kind"] == "promote":
            if approve:
                msg = gitflow.promote(db) or "promoted"
                runs.resolve_attention(db, aid, actor)
                return msg
            runs.resolve_attention(db, aid, actor)
            return "left on mesh/integration"
        runs.resolve_attention(db, aid, actor)  # question / error / escalation: acknowledged
        return "acknowledged"

    # -- planner
    def start_planner(self, run) -> list[str]:
        key = ("plan", run["id"])
        if key in self.futs or run["plan_attempts"] > 0:
            return []
        with self.db.tx() as c:
            c.execute("UPDATE runs SET plan_attempts=plan_attempts+1 WHERE id=?", (run["id"],))
        fb = (self.db.one("SELECT value FROM meta WHERE key=?", (f"plan_feedback_{run['id']}",)) or [None])[0] or None
        self.futs[key] = (self.pool.submit(_plan_job, self.paths, run["id"], run["cli"], fb, self.cfg()["brain"]["plan_timeout_seconds"]), {})
        self.db.event("conductor", "plan.started", None, {"run": run["id"], "cli": run["cli"]})
        return ["plan:start"]

    # -- harvest finished background jobs
    def collect_limits(self) -> None:
        """Limits jobs run with or without an active run, so they are harvested separately from run-bound jobs."""
        for key, (fut, meta) in list(self.futs.items()):
            if key[0] == "limits" and fut.done():
                del self.futs[key]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"error": repr(e)[:200]}
                self.db.event("conductor", "limits.done", None, res if isinstance(res, dict) else {})

    def collect(self, run) -> list[str]:
        acts = []
        for key, (fut, meta) in list(self.futs.items()):
            if key[0] == "limits" or not fut.done():
                continue
            del self.futs[key]
            try:
                res = fut.result()
            except Exception as e:
                res = {"crash": repr(e)}
            kind = key[0]
            if kind == "plan":
                acts += self._plan_done(run, res)
            elif kind == "brain":
                acts += self._brain_done(run, res, meta)
            elif kind == "verify":
                acts += self._verify_done(run, key[1], res)
            elif kind == "final":
                acts += self._final_done(run, res)

        return acts

    def _plan_done(self, run, res) -> list[str]:
        db, rid = self.db, run["id"]
        if res.get("plan"):
            plan = res["plan"]
            mapping = brain.apply_plan(db, rid, plan)
            with db.tx() as c:
                c.execute("UPDATE runs SET plan_json=?, summary=?, error=NULL WHERE id=?", (json.dumps(plan), brain.plan_summary(plan), rid))
            if self.cfg()["autonomy"].get("plan_approval", True):
                runs.set_status(db, rid, "awaiting_approval", "plan ready")
                runs.add_attention(db, rid, "plan_approval", str(rid), f"Plan ready: {brain.plan_summary(plan)}", json.dumps(plan)[:1800], {"tasks": len(mapping)})
            else:
                runs.set_status(db, rid, "executing", "plan auto-approved")
            return ["plan:done"]
        why = "; ".join(res.get("errors") or []) or res.get("error") or res.get("crash") or "the model did not return a JSON plan"
        with db.tx() as c:
            c.execute("UPDATE runs SET error=? WHERE id=?", (why[:500], rid))
        runs.add_attention(db, rid, "error", f"plan-{rid}", "Planning failed", f"{why}\nType /replan to try again, or /cli <name> to use another CLI.")
        db.event("conductor", "plan.failed", None, {"run": rid, "why": why[:200]})
        return ["plan:failed"]

    # -- dispatch
    def dispatch(self, run, cfg) -> list[str]:
        db, acts = self.db, []
        W, C = cfg["workers"], cfg["conductor"]
        for t in db.q("SELECT * FROM tasks WHERE run_id=? AND status='NEEDS_REWORK' AND kind NOT IN ('epic','project')", (run["id"],)):
            if not db.one("SELECT 1 FROM workers WHERE task_id=? AND state NOT IN ('FAILED','COMPLETED','TERMINATED')", (t["id"],)):
                tasks.transition(db, t["id"], "READY", "conductor", "re-dispatching after rework", force=True)
        for t in [x for x in tasks.ready(db) if x["run_id"] == run["id"]]:
            if workers.active_count(db) >= W["max_workers"]:
                break
            failed = db.one("SELECT COUNT(*) FROM workers WHERE task_id=? AND state='FAILED'", (t["id"],))[0]
            if failed >= C["max_task_attempts"]:
                tasks.transition(db, t["id"], "BLOCKED", "conductor", f"failed {failed} times", force=True)
                runs.add_attention(db, run["id"], "escalation", t["id"], f"'{t['title']}' failed {failed} times", self._why_failed(t["id"]), {"brain_tried": False})
                acts.append(f"block:{t['id']}")
                continue
            try:
                w = workers.spawn(db, t["id"], t["agent"], cli=t["prefer_cli"] or run["cli"], actor="conductor")
                acts.append(f"spawn:{w['id']}")
            except MeshError as e:
                msg = str(e)
                if "ownership conflict" in msg or "worker cap" in msg:
                    continue  # transient: try again next tick
                db.event("conductor", "dispatch.error", t["id"], {"task": t["id"], "error": msg[:300]})
                runs.add_attention(db, run["id"], "error", f"dispatch-{t['id']}", f"Could not start '{t['title']}'", msg)
                acts.append(f"dispatch-error:{t['id']}")
        return acts

    def _why_failed(self, tid: str) -> str:
        rows = self.db.q("SELECT id, state_detail FROM workers WHERE task_id=? AND state='FAILED' ORDER BY started", (tid,))
        return "\n".join(f"{r['id']}: {r['state_detail']}" for r in rows)

    # -- review pipeline: verify -> approve/reject/escalate
    def review(self, run, cfg) -> list[str]:
        db, acts = self.db, []
        for t in db.q("SELECT * FROM tasks WHERE run_id=? AND status='REVIEW' AND kind NOT IN ('epic','project')", (run["id"],)):
            tid = t["id"]
            sha = verify.head_sha(db, t)
            # keyed by submission count: a resubmission (even of an identical commit after a rejection) must be evaluated again
            nsub = db.one("SELECT COUNT(*) FROM events WHERE type='task.submitted' AND task_id=?", (tid,))[0]
            if ("verify", tid) in self.futs or (tid, sha, nsub) in self.handled:
                continue
            v = verify.latest_verification(db, tid)
            if v and v["sha"] == sha:
                self.handled.add((tid, sha, nsub))
                acts += self._decide_review(run, cfg, t, v)
            else:
                self.futs[("verify", tid)] = (self.pool.submit(_verify_job, self.paths, tid), {})
                acts.append(f"verify:{tid}")
        return acts

    def _verify_done(self, run, tid: str, res) -> list[str]:
        if "crash" in res:
            runs.add_attention(self.db, run["id"], "error", f"verify-{tid}", "Verification crashed", res["crash"])
        return []  # the next review() pass reads the stored verification record and decides

    def _decide_review(self, run, cfg, t, v) -> list[str]:
        db, C = self.db, cfg["conductor"]
        tid, title = t["id"], t["title"]
        if not v["ok"]:
            rejections = db.one("SELECT COUNT(*) FROM events WHERE type='task.rejected' AND task_id=?", (tid,))[0]
            if rejections < C["max_rework"]:
                verify.reject(db, tid, "conductor", f"Automatic verification failed:\n{v['detail'][:1500]}")
                return [f"reject:{tid}"]
            runs.add_attention(db, run["id"], "escalation", tid, f"'{title}' keeps failing verification", v["detail"][:1500], {"brain_tried": False})
            return [f"escalate:{tid}"]
        if v["weak"]:
            runs.add_attention(db, run["id"], "task_review", tid, f"'{title}': no automated checks ran. Approve anyway?", v["detail"][:1500])
            return [f"ask:{tid}"]
        if verify._risky(db, t, cfg):
            runs.add_attention(db, run["id"], "task_review", tid, f"'{title}' is high-risk and needs your review", v["detail"][:1500])
            return [f"ask:{tid}"]
        if C["auto_approve"] != "verified-low-risk":
            runs.add_attention(db, run["id"], "task_review", tid, f"'{title}' passed verification. Approve and merge?", v["detail"][:1500])
            return [f"ask:{tid}"]
        verify.approve(db, tid, "conductor")
        r = gitflow.integrate(db, tid, "conductor")
        if not r["ok"]:
            fails = db.one("SELECT COUNT(*) FROM events WHERE type IN ('integrate.conflict','integrate.revert') AND task_id=?", (tid,))[0]
            if fails > C["max_rework"]:
                runs.add_attention(db, run["id"], "escalation", tid, f"'{title}' cannot be merged ({r['stage']})", r["detail"][:1500], {"brain_tried": False})
            return [f"approve:{tid}", f"integrate-failed:{tid}"]
        return [f"approve:{tid}", f"merge:{tid}"]

    # -- brain turns (judgement only)
    def maybe_brain(self, run, cfg) -> list[str]:
        db = self.db
        if any(k[0] == "brain" for k in self.futs) or now() < self.brain_backoff_until or now() - self.last_brain_end < cfg["brain"]["debounce_seconds"]:
            return []
        msgs = [m for m in db.q("SELECT * FROM messages WHERE recipient='role:orchestrator' AND acked=0 AND delivery_state!='failed' ORDER BY ts LIMIT 12")
                if (m["type"] in BRAIN_TYPES or m["sender"] == "human") and not json.loads(m["ack_by"] or "[]")]
        esc = [a for a in db.q("SELECT * FROM attention WHERE status='open' AND kind='escalation' ORDER BY id")
               if not json.loads(a["payload"] or "{}").get("brain_tried")]
        if not msgs and not esc:
            return []
        fresh = db.one("SELECT brain_turns FROM runs WHERE id=?", (run["id"],))
        if fresh["brain_turns"] >= cfg["brain"]["max_turns_per_run"]:
            runs.add_attention(db, run["id"], "error", "brain-budget", "Orchestrator turn budget used up", "Raise [brain].max_turns_per_run or handle open items yourself.")
            return []
        parts = [bus.render_untrusted(msgs)] if msgs else []
        for a in esc:
            parts.append(f"ESCALATION [{a['subject']}] {a['title']}\n{a['detail'][:800]}")
        q = "Decide what to do about the messages and escalations below. Prefer the smallest action that unblocks the work."
        prompt = brain.brain_prompt(db, run, "\n\n".join(parts), q)
        resume = run["brain_session"] if adapters.ADAPTERS[run["cli"]](db.paths).supports_resume else None
        self.futs[("brain", run["id"])] = (self.pool.submit(_brain_job, self.paths, run["id"], run["cli"], prompt, resume, cfg["brain"]["timeout_seconds"]),
                                           {"msgs": [m["id"] for m in msgs], "esc": [a["id"] for a in esc]})
        with db.tx() as c:
            c.execute("UPDATE runs SET brain_turns=brain_turns+1 WHERE id=?", (run["id"],))
        db.event("conductor", "brain.turn", None, {"messages": len(msgs), "escalations": len(esc)})
        return ["brain:start"]

    def _brain_done(self, run, res, meta) -> list[str]:
        db = self.db
        self.last_brain_end = now()
        if res.get("crash") or res.get("error") or not res.get("obj"):
            why = res.get("crash") or res.get("error") or "the model did not return JSON actions"
            self.brain_backoff_until = now() + (300 if res.get("limit") else 60)
            runs.add_attention(db, run["id"], "error", f"brain-{int(now() // 300)}", "The orchestrator could not be reached", f"{why}\nOpen items stay pending; try /cli <name> to switch.")
            return ["brain:error"]
        if res.get("session"):
            with db.tx() as c:
                c.execute("UPDATE runs SET brain_session=? WHERE id=?", (res["session"], run["id"]))
        actions, errs = brain.validate_actions(res["obj"])
        log = brain.execute_actions(db, run, actions)
        for mid in meta.get("msgs", []):
            try:
                bus.ack(db, "orchestrator", mid)
            except MeshError:
                pass
        for aid in meta.get("esc", []):
            row = db.one("SELECT payload FROM attention WHERE id=?", (aid,))
            if row:
                p = json.loads(row["payload"] or "{}"); p["brain_tried"] = True
                with db.tx() as c:
                    c.execute("UPDATE attention SET payload=? WHERE id=?", (json.dumps(p), aid))
        if errs:
            runs.add_attention(db, run["id"], "error", f"brain-actions-{int(now())}", "Some orchestrator actions were invalid and skipped", "\n".join(errs))
        db.event("conductor", "brain.done", None, {"actions": len(actions), "log": log[:5]})
        return [f"brain:{len(actions)}-actions"]

    # -- completion
    def completion(self, run) -> list[str]:
        db = self.db
        leaves = [t for t in db.q("SELECT * FROM tasks WHERE run_id=? AND status!='CANCELLED'", (run["id"],))
                  if t["kind"] not in LEAF_SKIP and not db.one("SELECT 1 FROM tasks WHERE parent_id=?", (t["id"],))]
        if leaves and all(t["status"] == "COMPLETED" for t in leaves) and workers.active_count(db) == 0:
            runs.set_status(db, run["id"], "integrating", "all tasks completed")
            return ["run:integrating"]
        return []

    def start_final(self, run) -> list[str]:
        key = ("final", run["id"])
        if key in self.futs:
            return []
        self.futs[key] = (self.pool.submit(_final_job, self.paths, run["id"]), {})
        return ["final:start"]

    def _final_done(self, run, res) -> list[str]:
        db, rid = self.db, run["id"]
        cfg = self.cfg()
        configured = bool(cfg["validation"]["test"])
        with db.tx() as c:
            c.execute("UPDATE runs SET final_validation=? WHERE id=?", (("PASS" if res.get("ok") else "FAIL") + ("" if configured else " (no test command configured)") + "\n" + (res.get("out") or "")[-800:], rid))
        if res.get("ok"):
            runs.set_status(db, rid, "complete", "validated")
            base = gitflow.base_branch(db)
            runs.add_attention(db, rid, "promote", str(rid), f"Run complete. Promote mesh/integration to {base}?",
                               "Everything is merged and validated on the integration branch." + ("" if configured else " (No project test command is configured, so validation was skipped.)"))
            return ["run:complete"]
        runs.set_status(db, rid, "failed", "final validation failed", error=(res.get("out") or "")[-300:])
        runs.add_attention(db, rid, "error", f"final-{rid}", "Final validation failed on the integration branch", (res.get("out") or "")[-1500:])
        return ["run:failed"]

    # -- main loop
    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, "stop_flag", True))
        signal.signal(signal.SIGINT, lambda *a: setattr(self, "stop_flag", True))
        tick = self.cfg()["conductor"]["tick_seconds"]
        idle_exit = self.cfg()["conductor"]["idle_exit_seconds"]
        while not self.stop_flag:
            try:
                acts = self.tick()
            except Exception as e:  # never die on a scheduling bug; surface it
                self.db.event("conductor", "conductor.error", None, {"error": repr(e)[:300]})
                acts = []
            busy = acts or runs.active_run(self.db) or runs.pending_commands(self.db) or self.futs
            if busy:
                self._idle_since = now()
            elif now() - self._idle_since > idle_exit:
                break
            v = self.db.one("PRAGMA data_version")[0]
            end = time.time() + tick
            while time.time() < end and not self.stop_flag and self.db.one("PRAGMA data_version")[0] == v:
                time.sleep(0.2)


def serve(paths: Paths) -> int:
    lock = acquire_lock(paths)
    if lock is None:
        print("a conductor is already running for this project", file=sys.stderr)
        return 3
    c = Conductor(paths)
    try:
        c.run_forever()
    finally:
        c.close()
        lock.close()
    return 0
