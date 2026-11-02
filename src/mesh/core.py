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
                n = c.execute("UPDATE sessions SET status='ended', ended=? WHERE role='orchestrator' AND status IN ('active','stale') AND id!=?", (t, sid)).rowcount
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
