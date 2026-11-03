"""Config, sessions, and git helpers shared by the other modules."""
from __future__ import annotations

import os
import socket
import subprocess
import tomllib
from pathlib import Path

from .db import DB, MeshError, Paths, now

DEFAULT_CONFIG = {
    "project": {"name": "", "profile": "generic"},
    "git": {"integration_base": "", "integration_branch": "mesh/integration"},
    "validation": {"test": "", "lint": ""},
    "locks": {"ttl_seconds": 900},
    "tasks": {"stale_after_seconds": 1800},
    "context": {"warn_pct": 70, "checkpoint_pct": 85, "brief_budget_chars": 6000},
    "policy": {"destructive": "deny", "secret_in_write": "deny", "protect_mesh_files": True, "review_paths": []},
    "autonomy": {"mode": "worktree-confined", "accepted_by": "", "accepted_at": "", "accepted_cli_flags": "", "plan_approval": True},
    "workers": {"max_workers": 6, "max_depth": 2, "max_restarts": 2, "stall_seconds": 600, "worker_timeout_seconds": 3600,
                "max_total_cost_usd": 0, "max_budget_usd_per_worker": 0, "max_ai_credits_per_worker": 0,
                "require_committed_config": True, "default_route": ["claude", "copilot", "antigravity"], "route": {},
                "env_passthrough": ["ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
                                    "COPILOT_*", "CLAUDE_CODE_*", "FAKE_*", "MESH_BIN_*", "PYTHONPATH"]},
    "billing": {"claude": "unknown", "copilot": "unknown", "antigravity": "unknown"},
    "sessions": {"stale_after": 1800},
    "app": {"cli": ""},
    "limits": {"enabled": False, "refresh_seconds": 600},
    "conductor": {"auto_approve": "verified-low-risk", "max_rework": 2, "max_task_attempts": 3, "auto_review": False,
                  "idle_exit_seconds": 900, "tick_seconds": 1.0},
    "brain": {"timeout_seconds": 600, "max_turns_per_run": 40, "debounce_seconds": 3, "plan_timeout_seconds": 900},
}


def load_config(paths: Paths) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    if paths.config.exists():
        try:
            user = tomllib.loads(paths.config.read_text())
        except tomllib.TOMLDecodeError as e:
            raise MeshError(f"invalid {paths.config}: {e}")
        for k, v in user.items():
            cfg.setdefault(k, {}).update(v if isinstance(v, dict) else {})
    return cfg


# ---------------------------------------------------------------- sessions

def register_session(db: DB, cli: str, agent: str | None = None, native_id: str | None = None,
                     pid: int | None = None, model: str | None = None, role: str | None = None,
                     session_id: str | None = None, worker_id: str | None = None) -> str:
    """Idempotent: the same (cli, native_id) always maps to the same mesh session id."""
    sid = session_id or (f"{cli}-{native_id[:8]}" if native_id else f"{cli}-{os.urandom(4).hex()}")
    t = now()
    with db.tx() as c:
        row = c.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone()
        if row:
            c.execute("UPDATE sessions SET last_seen=?, status='active', ended=NULL, agent=COALESCE(?,agent), "
                      "model=COALESCE(?,model), pid=COALESCE(?,pid) WHERE id=?", (t, agent, model, pid, sid))
            db.event(sid, "session.resume", None, {"cli": cli}, conn=c)
        else:
            c.execute("INSERT INTO sessions(id,cli,agent,native_id,pid,host,model,started,last_seen,role,worker_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, cli, agent, native_id, pid, socket.gethostname(), model, t, t, role, worker_id))
            if role == "orchestrator":  # exactly one live orchestrator: retire older ones so role-addressed messages have one target
                n = c.execute("UPDATE sessions SET status='ended', ended=?, hidden='cleared' WHERE role='orchestrator' AND status IN ('active','stale') AND id!=?", (t, sid)).rowcount
                if n:
                    db.event(sid, "session.orchestrator_replaced", None, {"retired": n}, conn=c)
            db.event(sid, "session.start", None, {"cli": cli, "agent": agent}, conn=c)
    return sid


def set_toml_section(path, section: str, values: dict) -> None:
    """Replace or append one flat [section] in a TOML file (keeps everything else byte-for-byte)."""
    import re as _re
    def fmt(v):
        return "true" if v is True else "false" if v is False else f'"{v}"' if isinstance(v, str) else str(v)
    body = f"[{section}]\n" + "".join(f"{k} = {fmt(v)}\n" for k, v in values.items())
    text = path.read_text() if path.exists() else ""
    rx = _re.compile(rf"^\[{_re.escape(section)}\]\n(?:(?!\[).*\n?)*", _re.M)
    new = rx.sub(body, text, count=1) if rx.search(text) else text.rstrip("\n") + "\n\n" + body
    from .db import atomic_write
    atomic_write(path, new)
    _record_good(path)


def _record_good(path) -> None:
    """Remember the last config mesh itself wrote (hash + copy) so out-of-band edits can be detected and compared."""
    import hashlib, shutil
    try:
        d = path.parent / "runtime"
        d.mkdir(parents=True, exist_ok=True)
        (d / "mesh.toml.sha").write_text(hashlib.sha256(path.read_bytes()).hexdigest())
        shutil.copy2(path, d / "mesh.toml.last-good")
    except OSError:
        pass


def config_drift(paths) -> dict | None:
    import hashlib, tomllib
    d = paths.runtime
    sha_f, good_f = d / "mesh.toml.sha", d / "mesh.toml.last-good"
    if not sha_f.exists() or not paths.config.exists():
        return None
    cur = hashlib.sha256(paths.config.read_bytes()).hexdigest()
    if cur == sha_f.read_text().strip():
        return None
    changes = []
    try:
        old, new = tomllib.loads(good_f.read_text()), tomllib.loads(paths.config.read_text())
        for sec in sorted(set(old) | set(new)):
            for k in sorted(set(old.get(sec, {}) if isinstance(old.get(sec), dict) else {}) | set(new.get(sec, {}) if isinstance(new.get(sec), dict) else {})):
                a, b = (old.get(sec) or {}).get(k), (new.get(sec) or {}).get(k)
                if a != b:
                    changes.append(f"{sec}.{k}: {a!r} -> {b!r}")
    except Exception:
        changes = ["(could not compare)"]
    return {"sha": cur, "changes": changes or ["(formatting only)"], "backup": str(good_f)}


def heartbeat(db: DB, sid: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now(), sid))


def end_session(db: DB, sid: str, status: str = "ended") -> None:
    from . import locks
    with db.tx() as c:
        c.execute("UPDATE sessions SET status=?, ended=? WHERE id=?", (status, now(), sid))
        db.event(sid, "session.end", None, {"status": status}, conn=c)
    # keep task ownership (so it is recoverable/reassignable) but free file locks
    locks.release(db, sid)


def mark_stale(db: DB, stale_after: float = 1800) -> int:
    """Age out interactive/orchestrator sessions that went silent (e.g. Antigravity has no SessionEnd hook).
    Worker sessions are excluded: their liveness is owned by the worker runner/reconcile."""
    t = now()
    with db.tx() as c:
        rows = c.execute("SELECT id FROM sessions WHERE status='active' AND COALESCE(role,'')!='worker' AND last_seen < ?", (t - stale_after,)).fetchall()
        for r in rows:
            c.execute("UPDATE sessions SET status='stale' WHERE id=?", (r["id"],))
        if rows:
            db.event("mesh", "session.stale", None, {"count": len(rows)}, conn=c)
    return len(rows)


LIVE_WORKER_STATES = ("INITIALISING", "PLANNING", "WORKING", "COMMUNICATING", "REVIEWING", "BLOCKED", "PAUSED", "IDLE")


def _signal_cli_process(pid: int | None, cli: str) -> bool:
    """Best effort: SIGTERM an interactive CLI process only if it really looks like that CLI (never a recycled pid)."""
    import os as _os, signal as _sig, subprocess as _sp
    if not pid or pid == _os.getpid():
        return False
    cmd = _sp.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout.lower()
    marker = {"antigravity": ("agy", "antigravity"), "claude": ("claude",), "copilot": ("copilot",)}.get(cli, (cli,))
    if not cmd or not any(m in cmd for m in marker):
        return False
    try:
        _os.kill(pid, _sig.SIGTERM)
        return True
    except OSError:
        return False


def kill_session(db: DB, sid: str, actor: str = "human") -> str:
    """Stop a session (live, idle or finished) and hide it. Workers are terminated; their unfinished task is parked as BLOCKED
    (not silently re-dispatched) so the user decides with /retry. Interactive CLI sessions get a best-effort SIGTERM."""
    from . import tasks as _tasks, workers as _w
    w = db.one("SELECT * FROM workers WHERE id=?", (sid,))
    s = db.one("SELECT * FROM sessions WHERE id=?", (sid,))
    if not w and not s:
        raise MeshError(f"unknown session {sid}")
    note = ""
    if w:
        if w["state"] not in _w.TERMINAL:
            _w.terminate(db, sid, actor, "killed by user")
        t = db.one("SELECT status, session_id FROM tasks WHERE id=?", (w["task_id"],)) if w["task_id"] else None
        if t and t["status"] in ("READY", "ASSIGNED", "IN_PROGRESS") and t["session_id"] in (None, sid):
            _tasks.transition(db, w["task_id"], "BLOCKED", actor, "worker killed by user; /retry to run it again", force=True)
            note = f"; task {w['task_id']} is parked (use /retry to run it again)"
        with db.tx() as c:
            c.execute("UPDATE workers SET hidden='killed' WHERE id=?", (sid,))
            c.execute("UPDATE sessions SET hidden='killed', status='killed', ended=COALESCE(ended,?) WHERE id=?", (now(), sid))
    else:
        if s["role"] == "orchestrator":
            raise MeshError("the orchestrator belongs to the run: use /pause or /halt, or /cli to switch it")
        sig = _signal_cli_process(s["pid"], s["cli"]) if s["status"] == "active" else False
        with db.tx() as c:
            c.execute("UPDATE sessions SET hidden='killed', status='killed', ended=COALESCE(ended,?) WHERE id=?", (now(), sid))
            db.event(actor, "session.killed", None, {"session": sid, "signalled": sig}, conn=c)
        note = "; its CLI process was asked to stop" if sig else ""
    db.event(actor, "session.killed", None, {"session": sid})
    return f"killed {sid}{note}"


def clear_sessions(db: DB, scope: str, actor: str = "human") -> int:
    """Hide finished/idle sessions from the live list. scope: a session id | 'done' | 'idle' | 'all' (everything not live)."""
    from . import workers as _w
    n = 0
    with db.tx() as c:
        if scope not in ("done", "idle", "all"):
            r = c.execute("SELECT state FROM workers WHERE id=?", (scope,)).fetchone()
            srow = c.execute("SELECT status, role FROM sessions WHERE id=?", (scope,)).fetchone()
            if not r and not srow:
                raise MeshError(f"unknown session {scope}")
            live = (r and r["state"] in LIVE_WORKER_STATES) or (not r and srow["status"] == "active")
            if live:
                raise MeshError("that session is live: kill it instead (/kill), or wait until it is done")
            c.execute("UPDATE workers SET hidden='cleared' WHERE id=? AND hidden IS NULL", (scope,))
            n = c.execute("UPDATE sessions SET hidden='cleared' WHERE id=? AND hidden IS NULL", (scope,)).rowcount
        else:
            wstates = {"done": ("COMPLETED",), "idle": (), "all": ("COMPLETED", "FAILED", "TERMINATED")}[scope]
            if scope == "done":  # a worker ended by approval of its verified task is "done", not "killed"
                c.execute("UPDATE workers SET hidden='cleared' WHERE hidden IS NULL AND state='TERMINATED' AND ended_reason LIKE 'task verified%'")
            sstat = {"done": ("ended",), "idle": ("stale",), "all": ("ended", "stale", "failed", "crashed", "terminated", "rolled-over")}[scope]
            if wstates:
                ph = ",".join("?" * len(wstates))
                c.execute(f"UPDATE workers SET hidden='cleared' WHERE hidden IS NULL AND state IN ({ph})", wstates)
            ph = ",".join("?" * len(sstat))
            n = c.execute(f"UPDATE sessions SET hidden='cleared' WHERE hidden IS NULL AND role IS NOT 'orchestrator' AND status IN ({ph})", sstat).rowcount
        db.event(actor, "session.cleared", None, {"scope": scope, "n": n}, conn=c)
    return n


def resolve_actor(db: DB, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    for var in ("MESH_SESSION", "MESH_ACTOR"):
        if os.environ.get(var):
            return os.environ[var]
    # Only unlabelled interactive sessions can be "me"; worker/orchestrator sessions always announce themselves via MESH_SESSION,
    # so a human typing `mesh approve` can never be mistaken for the only running worker.
    rows = db.q("SELECT id FROM sessions WHERE status='active' AND role IS NULL ORDER BY last_seen DESC")
    if len(rows) == 1:
        return rows[0]["id"]
    return "human"


# ---------------------------------------------------------------- git

def git(root: Path, *args: str, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd or root, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise MeshError(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r


def is_git(root: Path) -> bool:
    return git(root, "rev-parse", "--git-dir", check=False).returncode == 0


def git_state(root: Path) -> dict:
    if not is_git(root):
        return {"git": False}
    head = git(root, "rev-parse", "HEAD", check=False)
    br = git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    st = git(root, "status", "--porcelain", check=False).stdout.splitlines()
    return {"git": True, "head": head.stdout.strip() if head.returncode == 0 else None,
            "branch": br.stdout.strip() if br.returncode == 0 else None,
            "dirty": [l[3:] for l in st if not l[3:].startswith(".mesh/runtime")]}
