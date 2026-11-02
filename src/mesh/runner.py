"""Detached per-worker supervisor (`mesh worker run <id>`). One runner = one worker; no central daemon.

The runner owns the CLI child process for that worker: launches it headless in the worker's worktree, parses its
stream into events/state/usage, heartbeats, watches for stalls/timeouts, classifies the exit and applies the exit
policy (restart / fallback CLI / fail). The DB is the only rendezvous with the rest of the system.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import adapters, checkpoint, locks, pricing, streams, tasks, usage, workers
from .core import load_config
from .db import DB, MeshError, Paths, find_root, now


def child_env(cfg: dict, w, extra: dict) -> dict:
    """Allow-listed environment: no ambient secrets leak to workers except explicitly passed-through names."""
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_CACHE_HOME", "SSH_AUTH_SOCK", "GIT_CONFIG_GLOBAL")
    pats = cfg["workers"]["env_passthrough"]
    env = {k: v for k, v in os.environ.items() if k in keep or any(fnmatch.fnmatch(k, p) for p in pats)}
    env.update({"MESH_SESSION": w["id"], "MESH_WORKER": w["id"], "MESH_ROLE": "worker", "MESH_AGENT": w["agent"] or "",
                "MESH_ROOT": os.environ.get("MESH_ROOT", ""), "MESH_TASK": w["task_id"] or ""})
    env.update(extra)
    return env


class Acc:
    """Cumulative usage for one worker across attempts, merging 'delta' (per-message) and 'cumulative' (running total) scopes."""
    def __init__(self):
        self.i = self.o = self.cr = self.cc = 0
        self.seen = False
        self.ctx = None
        self.ctx_size = None
        self.cost = None
        self.cost_base = 0.0
        self.model = None
        self.base = [0, 0, 0, 0]  # totals from earlier attempts
        self.cur_cum = None

    def add(self, e: dict):
        self.seen = True
        if e.get("model"):
            self.model = e["model"]
        if e.get("scope") == "cumulative":
            self.cur_cum = (e.get("input") or 0, e.get("output") or 0, e.get("cache_read") or 0, 0)
        else:
            self.i += e.get("input") or 0
            self.o += e.get("output") or 0
            self.cr += e.get("cache_read") or 0
            self.cc += e.get("cache_create") or 0
        if e.get("context_tokens"):
            self.ctx = e["context_tokens"]

    def final(self, e: dict):
        u = e.get("usage")
        if u:  # authoritative end-of-run totals replace running sums
            self.seen = True
            if e.get("scope") != "x":
                self.cur_cum = (u.get("input") or 0, u.get("output") or 0, u.get("cache_read") or 0, u.get("cache_create") or 0)
                self.i = self.o = self.cr = self.cc = 0
        if e.get("cost") is not None:
            self.cost = e["cost"]
        if e.get("context_size"):
            self.ctx_size = e["context_size"]

    def totals(self):
        c = self.cur_cum or (0, 0, 0, 0)
        return (self.base[0] + self.i + c[0], self.base[1] + self.o + c[1], self.base[2] + self.cr + c[2], self.base[3] + self.cc + c[3])

    def cost_total(self):
        return None if self.cost is None and not self.cost_base else self.cost_base + (self.cost or 0)

    def next_attempt(self):
        self.cost_base = self.cost_total() or 0.0
        self.cost = None
        self.base = list(self.totals())
        self.i = self.o = self.cr = self.cc = 0
        self.cur_cum = None


def write_usage(db: DB, cfg: dict, w, acc: Acc, run_cost_base: float) -> None:
    if not acc.seen and acc.cost_total() is None:
        return
    i, o, cr, cc = acc.totals()
    mode = pricing.billing_mode(cfg, w["cli"])
    reported = acc.cost_total()
    est = pricing.estimate_cost(cfg, acc.model or w["model"], i, o, cr) if reported is None else None
    cost = reported if reported is not None else est
    size = acc.ctx_size
    usage.record(db, w["id"], w["cli"], "reported", task_id=w["task_id"], agent=w["agent"], model=acc.model or w["model"],
                 worker_id=w["id"], billing_mode=mode, cost_source="reported" if reported is not None else ("estimated" if est is not None else "unavailable"),
                 input_tokens=i, output_tokens=o, cache_read=cr, cache_create=cc, context_tokens=acc.ctx, context_size=size,
                 context_pct=round(100 * acc.ctx / size, 1) if acc.ctx and size else None, cost_usd=cost)


def run(worker_id: str) -> int:
    paths = Paths(find_root())
    db = DB(paths)
    cfg = load_config(paths)
    stop = {"flag": False}
    child: dict = {"p": None}

    def on_term(sig, frm):
        stop["flag"] = True
        p = child["p"]
        if p and p.poll() is None:
            try:
                p.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    acc = Acc()
    code = 0
    try:
        while True:
            w = workers.get(db, worker_id)
            if w["state"] == "TERMINATED" or stop["flag"]:
                break
            res = attempt(db, cfg, w, acc, child, stop)
            if res["action"] in ("done", "terminated", "failed", "unsubmitted"):
                break
            if res["action"] == "fallback":
                acc = Acc()  # different provider: its usage/cost is a separate series, never mixed with the previous CLI's
            else:
                acc.next_attempt()
            time.sleep(0.2)
    except Exception as e:  # runner bugs must not leave a zombie "WORKING" row
        db.event(worker_id, "worker.runner_error", None, {"error": repr(e)[:300]})
        try:
            workers.handle_exit(db, worker_id, "crash", None, f"runner error: {e!r}")
        except Exception:
            pass
        code = 1
    finally:
        db.close()
    return code


def attempt(db: DB, cfg: dict, w, acc: Acc, child: dict, stop: dict) -> dict:
    adapter = adapters.ADAPTERS[w["cli"]](db.paths)
    run_dir = Path(w["run_dir"])
    resume_id = w["native_session"] if (w["state_detail"] == "resume" and adapter.supports_resume) else None
    prompt = (run_dir / "prompt.md").read_text() if (run_dir / "prompt.md").exists() else workers.build_prompt(db, w)
    if w["restarts"] > 0 or resume_id:
        brief = checkpoint.brief(db, w["task_id"], w["id"])
        prompt = (f"Continue the task; the previous run ended early. Verify state with `git status`/`git log`, do not repeat finished steps.\n\n{brief}"
                  if resume_id else f"{prompt}\n\n---\n{brief}")
    cmd, extra = adapter.headless_cmd(prompt, w["model"], resume_id, w["agent"], cfg, str(run_dir))
    t = tasks.get(db, w["task_id"])
    if t["status"] == "ASSIGNED":
        tasks.transition(db, t["id"], "IN_PROGRESS", w["id"])
    elif t["status"] == "NEEDS_REWORK":
        tasks.transition(db, t["id"], "IN_PROGRESS", w["id"])
    workers.set_state(db, w["id"], "PLANNING", f"starting {w['cli']}", last_output=now(), pid=None)
    stream = open(run_dir / "stream.jsonl", "ab")
    err = open(run_dir / "stderr.log", "ab")
    p = subprocess.Popen(cmd, cwd=w["worktree"], env=child_env(cfg, w, extra), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=err, text=True, bufsize=1)
    child["p"] = p
    with db.tx() as c:
        c.execute("UPDATE workers SET pid=? WHERE id=?", (p.pid, w["id"]))
    st = {"last": time.time(), "limit": None, "result": None, "stalled": False, "timeout": False, "dead": False}
    wd = threading.Thread(target=watchdog, args=(db.paths, cfg, w["id"], p, st), daemon=True)
    wd.start()
    run_cost_base = 0.0
    last_usage_write = 0.0
    for line in p.stdout:
        st["last"] = time.time()
        stream.write(line.encode()); stream.flush()
        for e in streams.parse_line(w["cli"], line):
            k = e.get("kind")
            if k == "init":
                upd = {}
                if e.get("session"):
                    upd["native_session"] = e["session"]
                workers.set_state(db, w["id"], "WORKING", None, last_output=now(), **upd)
                if e.get("session"):
                    with db.tx() as c:
                        c.execute("UPDATE sessions SET native_id=?, model=COALESCE(?,model) WHERE id=?", (e["session"], e.get("model"), w["id"]))
            elif k == "usage":
                acc.add(e)
                if e.get("session"):
                    with db.tx() as c:
                        c.execute("UPDATE workers SET native_session=COALESCE(native_session,?) WHERE id=?", (e["session"], w["id"]))
            elif k == "tool":
                cmdtxt = json.dumps(e.get("input") or "")
                state = "COMMUNICATING" if re.search(r"mesh (msg|worker request)", cmdtxt) else \
                    "REVIEWING" if re.search(r"mesh (task submit|review)", cmdtxt) else "WORKING"
                with db.tx() as c:
                    c.execute("UPDATE workers SET state=?, last_output=?, last_event=?, last_activity=? WHERE id=?",
                              (state, now(), now(), f"{e.get('name')}"[:60], w["id"]))
                db.event(w["id"], "tool", w["task_id"], {"tool": e.get("name")})
            elif k == "result":
                st["result"] = e
                acc.final(e)
                if e.get("session"):
                    with db.tx() as c:
                        c.execute("UPDATE workers SET native_session=COALESCE(?,native_session) WHERE id=?", (e["session"], w["id"]))
            elif k in ("limit", "error"):
                if k == "limit":
                    st["limit"] = e.get("detail") or "limit"
                db.event(w["id"], f"worker.{k}", w["task_id"], {"detail": (e.get("detail") or "")[:200], "retry": bool(e.get("retry"))})
            with db.tx() as c:
                c.execute("UPDATE workers SET last_output=? WHERE id=?", (now(), w["id"]))
        if time.time() - last_usage_write > 10:
            write_usage(db, cfg, w, acc, run_cost_base)
            last_usage_write = time.time()
    rc = p.wait()
    st["dead"] = True
    child["p"] = None
    # Copilot token usage lives in OTel output, not the stream
    otel = run_dir / "otel.jsonl"
    if w["cli"] == "copilot" and otel.exists():
        tot = streams.parse_otel_file(otel.read_text(errors="ignore"))
        if tot:
            acc.add({"kind": "usage", "scope": "cumulative", "input": tot["input"], "output": tot["output"]})
    write_usage(db, cfg, w, acc, run_cost_base)
    stream.close(); err.close()
    stderr_tail = (run_dir / "stderr.log").read_bytes()[-2000:].decode(errors="ignore")
    if stop["flag"] or workers.get(db, w["id"])["state"] == "TERMINATED":
        return {"action": "terminated"}
    res = st["result"]
    if st["timeout"]:
        outcome, detail = "timeout", "worker exceeded its time limit"
    elif st["limit"] or (rc != 0 and streams.classify_error(stderr_tail) == "limit"):
        outcome, detail = "limit", st["limit"] or stderr_tail[-200:]
    elif rc == 0 and (res is None or res.get("ok")):
        outcome, detail = "clean", (res or {}).get("text", "")[:200]
    else:
        outcome, detail = "crash", (res or {}).get("error") or stderr_tail[-200:] or f"exit {rc}"
    return workers.handle_exit(db, w["id"], outcome, rc, str(detail))


def watchdog(paths: Paths, cfg: dict, wid: str, p: subprocess.Popen, st: dict) -> None:
    """Heartbeat, lock keep-alive, stall detection and hard timeout. Own DB connection (thread safety)."""
    db = DB(paths)
    started = time.time()
    W = cfg["workers"]
    try:
        while not st["dead"] and p.poll() is None:
            time.sleep(min(5, max(0.5, W["stall_seconds"] / 4)))
            with db.tx() as c:
                c.execute("UPDATE workers SET last_event=? WHERE id=?", (now(), wid))
            locks.heartbeat(db, wid, cfg["locks"]["ttl_seconds"])
            core_hb = db.conn
            with db.tx() as c:
                c.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now(), wid))
            if (db.one("SELECT state FROM workers WHERE id=?", (wid,)) or ["TERMINATED"])[0] == "TERMINATED":
                p.terminate()
                return
            idle = time.time() - st["last"]
            if idle > W["stall_seconds"] and not st["stalled"]:
                st["stalled"] = True
                db.event(wid, "worker.stalled", None, {"idle_seconds": int(idle)})
                bus_send_stall(db, wid, idle)
            elif idle <= W["stall_seconds"]:
                st["stalled"] = False
            if time.time() - started > W["worker_timeout_seconds"]:
                st["timeout"] = True
                p.terminate()
                return
    finally:
        db.close()


def bus_send_stall(db: DB, wid: str, idle: float) -> None:
    from . import bus
    w = workers.get(db, wid)
    bus.send(db, wid, "role:orchestrator", "blocker", f"worker {wid} has produced no output for {int(idle)}s (task {w['task_id']}).",
             w["task_id"], 2, dedupe_key=f"stall-{wid}-{int(time.time() // 600)}")
