"""Worktree isolation and the integration workflow (validate -> conflict check -> merge -> re-validate)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import bus, tasks
from .core import git, load_config
from .db import DB, MeshError


def base_branch(db: DB) -> str:
    cfg = load_config(db.paths)
    b = cfg["git"]["integration_base"]
    if b:
        return b
    for cand in ("main", "master"):
        if git(db.paths.root, "rev-parse", "--verify", "-q", cand, check=False).returncode == 0:
            return cand
    return git(db.paths.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def worktree_create(db: DB, task_id: str, base: str | None = None) -> Path:
    t = tasks.get(db, task_id)
    root = db.paths.root
    base = base or base_branch(db)
    branch = f"mesh/{task_id}"
    path = db.paths.worktrees / task_id
    if path.exists():
        return path
    db.paths.worktrees.mkdir(parents=True, exist_ok=True)
    exists = git(root, "rev-parse", "--verify", "-q", branch, check=False).returncode == 0
    args = ["worktree", "add", str(path)] + ([branch] if exists else ["-b", branch, base])
    git(root, *args)
    with db.tx() as c:
        c.execute("UPDATE tasks SET branch=? WHERE id=?", (branch, task_id))
        db.event(t["session_id"] or "mesh", "worktree.create", task_id, {"branch": branch}, conn=c)
    return path


def worktree_remove(db: DB, task_id: str) -> None:
    path = db.paths.worktrees / task_id
    if path.exists():
        git(db.paths.root, "worktree", "remove", "--force", str(path))


def run_validation(cwd: Path, cfg: dict) -> tuple[bool, str]:
    cmd = cfg["validation"]["test"]
    if not cmd:
        return True, "(no test command configured; validation skipped — configure [validation].test in mesh.toml)"
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=1800)
    return r.returncode == 0, (r.stdout + r.stderr)[-3000:]


def _integration_wt(db: DB, cfg: dict, base: str) -> Path:
    root = db.paths.root
    branch = cfg["git"]["integration_branch"]
    path = db.paths.worktrees / "_integration"
    if not path.exists():
        db.paths.worktrees.mkdir(parents=True, exist_ok=True)
        exists = git(root, "rev-parse", "--verify", "-q", branch, check=False).returncode == 0
        git(root, "worktree", "add", str(path), *([branch] if exists else ["-b", branch, base]))
    return path


def integrate(db: DB, task_id: str, actor: str = "integrator") -> dict:
    """Never silently discards work. Returns {'ok': bool, 'stage': str, 'detail': str}."""
    cfg = load_config(db.paths)
    root = db.paths.root
    t = tasks.get(db, task_id)
    branch = t["branch"]
    if not branch:
        raise MeshError(f"task {task_id} has no branch; use `mesh worktree create` first")
    wt = db.paths.worktrees / task_id
    if wt.exists():
        dirty = git(root, "status", "--porcelain", cwd=wt).stdout.strip()
        if dirty:
            return {"ok": False, "stage": "precheck", "detail": "worktree has uncommitted changes; commit them first"}
    base = base_branch(db)
    ok, out = run_validation(wt if wt.exists() else root, cfg)
    if not ok:
        _rework(db, t, actor, "validation failed on task branch", out)
        return {"ok": False, "stage": "validate", "detail": out}
    iw = _integration_wt(db, cfg, base)
    prev = git(root, "rev-parse", "HEAD", cwd=iw).stdout.strip()
    mt = git(root, "merge-tree", "--write-tree", "--name-only", "HEAD", branch, cwd=iw, check=False)
    if mt.returncode != 0:
        files = [l for l in mt.stdout.splitlines()[1:] if l and not l.startswith(("Auto-merging", "CONFLICT"))]
        detail = f"merge conflict integrating {branch}: {', '.join(files[:10]) or mt.stdout[:200]}"
        _rework(db, t, actor, "merge conflict", detail)
        bus.send(db, actor, t["session_id"] or "*", "conflict", detail, task_id, priority=1)
        db.event(actor, "integrate.conflict", task_id, {"detail": detail})
        return {"ok": False, "stage": "conflict", "detail": detail}
    m = git(root, "merge", "--no-ff", "-m", f"mesh: integrate {task_id} {t['title']}", branch, cwd=iw, check=False)
    if m.returncode != 0:
        git(root, "merge", "--abort", cwd=iw, check=False)
        _rework(db, t, actor, "merge failed", m.stderr)
        return {"ok": False, "stage": "merge", "detail": m.stderr}
    ok, out = run_validation(iw, cfg)
    if not ok:
        git(root, "reset", "--hard", prev, cwd=iw)  # only our own throwaway integration worktree
        _rework(db, t, actor, "tests failed after merge", out)
        db.event(actor, "integrate.revert", task_id, {"to": prev})
        return {"ok": False, "stage": "post-merge-validate", "detail": out}
    if t["status"] == "IN_PROGRESS":
        tasks.transition(db, task_id, "REVIEW", actor, "integrated; awaiting acceptance")
    tasks.transition(db, task_id, "COMPLETED", actor, f"integrated into {cfg['git']['integration_branch']}", force=True)
    db.event(actor, "integrate.ok", task_id, {"branch": branch})
    return {"ok": True, "stage": "done", "detail": f"merged into {cfg['git']['integration_branch']} (promote to {base} is a human step: `mesh promote`)"}


def _rework(db: DB, t, actor: str, why: str, detail: str) -> None:
    cur = tasks.get(db, t["id"])["status"]
    if cur in ("IN_PROGRESS", "REVIEW", "COMPLETED", "FAILED"):
        if cur == "IN_PROGRESS":
            tasks.transition(db, t["id"], "REVIEW", actor, "submitted for integration", force=True)
        tasks.transition(db, t["id"], "NEEDS_REWORK", actor, f"{why}: {detail[:300]}", force=True)


def promote(db: DB) -> str:
    cfg = load_config(db.paths)
    base = base_branch(db)
    r = git(db.paths.root, "merge", "--ff-only", cfg["git"]["integration_branch"], check=False)
    if r.returncode != 0:
        raise MeshError("cannot fast-forward " + base + ": " + (r.stderr or r.stdout).strip())
    return r.stdout.strip()
