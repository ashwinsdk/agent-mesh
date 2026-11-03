"""Completion gates: workers can only *submit*; the orchestrator/human verifies and approves against the actual commit."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import bus, locks, tasks, workers
from .core import git, is_git as core_is_git_root, load_config
from .db import DB, MeshError, now
from .gitflow import base_branch

REVIEWER_AGENTS = ("reviewer", "security", "qa")


def _wt(db: DB, t) -> Path | None:
    p = db.paths.worktrees / t["id"]
    return p if p.exists() else None


def head_sha(db: DB, t) -> str | None:
    if not t["branch"]:
        return None
    r = git(db.paths.root, "rev-parse", t["branch"], check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def changed_files(db: DB, t) -> list[str]:
    if not t["branch"]:
        return []
    r = git(db.paths.root, "diff", "--name-only", f"{base_branch(db)}...{t['branch']}", check=False)
    return [l for l in r.stdout.splitlines() if l]


def submit(db: DB, task_id: str, actor: str, note: str = "") -> str:
    t = tasks.get(db, task_id)
    if t["session_id"] and t["session_id"] != actor and actor not in ("human", "mesh"):
        raise MeshError(f"task {task_id} is owned by {t['session_id']}")
    if t["status"] == "ASSIGNED":
        tasks.transition(db, task_id, "IN_PROGRESS", actor)
        t = tasks.get(db, task_id)
    if t["status"] != "IN_PROGRESS":
        raise MeshError(f"task is {t['status']}; only IN_PROGRESS work can be submitted")
    wt = _wt(db, t)
    if wt:
        dirty = [l for l in git(db.paths.root, "status", "--porcelain", cwd=wt).stdout.splitlines() if l]
        if dirty:
            raise MeshError("uncommitted changes in your worktree; commit them first:\n  " + "\n  ".join(dirty[:10]))
    sha = head_sha(db, t)
    files = changed_files(db, t)
    with db.tx() as c:
        c.execute("UPDATE tasks SET submitted_sha=?, handoff=? WHERE id=?", (sha, note, task_id))
    tasks.transition(db, task_id, "REVIEW", actor, "submitted for review")
    db.event(actor, "task.submitted", task_id, {"sha": (sha or "")[:10], "files": len(files)})
    bus.send(db, actor, "role:orchestrator", "task_complete", f"task {task_id} submitted by {actor}. Files changed: {', '.join(files[:15]) or '-'}. Handoff: {note[:500]}",
             task_id, 1, requires_ack=True, dedupe_key=f"submit-{task_id}-{(sha or '')[:8]}")
    if db.one("SELECT 1 FROM workers WHERE id=?", (actor,)):
        workers.set_state(db, actor, "REVIEWING", "awaiting verification")
    return f"submitted {task_id} at {(sha or 'n/a')[:10]} ({len(files)} files); the orchestrator will verify it"


def _run(cmd: str, cwd: Path, timeout: int = 1800) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    return r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def verify(db: DB, task_id: str, actor: str) -> dict:
    """Ownership check + task verify command + project tests, recorded against the exact commit."""
    t = tasks.get(db, task_id)
    cfg = load_config(db.paths)
    wt = _wt(db, t) or db.paths.root
    sha = head_sha(db, t)
    owned = json.loads(t["files"] or "[]")
    files = changed_files(db, t)
    notes, ok = [], True
    if owned and files:
        bad = [f for f in files if not any(locks.overlaps(f, p) for p in owned)]
        if bad:
            ok = False
            notes.append(f"OWNERSHIP VIOLATION: changed files outside the task's declared files: {', '.join(bad[:10])}")
    notes.append(f"changed files ({len(files)}): {', '.join(files[:20]) or '-'}")
    checks, baseline_red = 0, False
    if t["verify_cmd"]:
        checks += 1
        passed, out = _run(t["verify_cmd"], wt)
        ok &= passed
        notes.append(f"[task verify] `{t['verify_cmd']}` -> {'PASS' if passed else 'FAIL'}\n{out.strip()[-600:]}")
        if cfg["validation"]["test"]:
            notes.append("[project test] not run per task: this task has its own verify command; the project tests run at merge time and at the end")
    elif cfg["validation"]["test"]:
        checks += 1
        passed, out = _run(cfg["validation"]["test"], wt)
        if not passed:
            from . import gitflow
            b_ok, _ = gitflow.baseline(db, cfg, gitflow._integration_wt(db, cfg, base_branch(db))) if core_is_git(db) else (True, "")
            if not b_ok:  # already failing before this task: do not blame it, but this is not strong evidence either
                passed, baseline_red = True, True
        ok &= passed
        notes.append(f"[project test] `{cfg['validation']['test']}` -> {'PASS' if passed else 'FAIL'}" + (" (already failing on the integration branch; not blamed on this task)" if baseline_red else "") + f"\n{out.strip()[-600:]}")
    weak = checks == 0 or baseline_red
    if weak:
        notes.append("WEAK: no verify command or test command configured; nothing was actually executed")
    detail = "\n".join(notes)
    with db.tx() as c:
        c.execute("INSERT INTO verifications(task_id,sha,ok,weak,detail,ts) VALUES(?,?,?,?,?,?)", (task_id, sha, 1 if ok else 0, 1 if weak else 0, detail, now()))
        db.event(actor, "verify.result", task_id, {"ok": ok, "weak": weak, "sha": (sha or "")[:10]}, conn=c)
    return {"ok": ok, "weak": weak, "detail": detail, "sha": sha}


def latest_verification(db: DB, task_id: str):
    return db.one("SELECT * FROM verifications WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))


def _risky(db: DB, t, cfg) -> bool:
    if t["risk"] == "high" or t["review_required"]:
        return True
    pats = cfg["policy"].get("review_paths", [])
    return bool(pats) and any(locks.overlaps(f, p) for f in changed_files(db, t) for p in pats)


def review(db: DB, task_id: str, actor: str, approve: bool, note: str = "") -> str:
    t = tasks.get(db, task_id)
    if actor == t["session_id"]:
        raise MeshError("the task's author cannot review its own work")
    with db.tx() as c:
        c.execute("INSERT INTO approvals(kind,subject,requested_by,payload,status,decided_by,ts,decided) VALUES('review',?,?,?,?,?,?,?)",
                  (task_id, t["session_id"], json.dumps({"note": note, "sha": head_sha(db, t)}), "approved" if approve else "rejected", actor, now(), now()))
    bus.send(db, actor, "role:orchestrator", "review", f"review of {task_id}: {'APPROVE' if approve else 'REJECT'}. {note[:500]}", task_id, 1, requires_ack=True)
    return "review recorded"


def _independent_review_ok(db: DB, t, sha) -> bool:
    for r in db.q("SELECT * FROM approvals WHERE kind='review' AND subject=? AND status='approved' ORDER BY id DESC", (t["id"],)):
        by = r["decided_by"]
        if by in ("human",):
            return True
        w = db.one("SELECT agent FROM workers WHERE id=?", (by,))
        if w and w["agent"] in REVIEWER_AGENTS and json.loads(r["payload"] or "{}").get("sha") in (sha, None):
            return True
    return False


def approve(db: DB, task_id: str, actor: str, accept_unverified: bool = False) -> str:
    t = tasks.get(db, task_id)
    cfg = load_config(db.paths)
    if t["status"] != "REVIEW":
        raise MeshError(f"task is {t['status']}; only tasks submitted for review (REVIEW) can be approved")
    v = latest_verification(db, task_id)
    sha = head_sha(db, t)
    if not v:
        raise MeshError(f"no verification on record: run `mesh verify {task_id}` first")
    if sha and v["sha"] != sha:
        raise MeshError(f"the branch moved since verification ({(v['sha'] or '')[:8]} -> {sha[:8]}); re-run `mesh verify {task_id}`")
    if not v["ok"]:
        raise MeshError("the latest verification FAILED; reject the task or fix and re-verify")
    if v["weak"] and not accept_unverified:
        raise MeshError("verification was WEAK (no checks executed); configure verify/test commands or pass --accept-unverified")
    if _risky(db, t, cfg) and not _independent_review_ok(db, t, sha):
        raise MeshError("high-risk task: needs an independent approving review (`mesh review approve` by a reviewer/security worker or the human)")
    owner = t["session_id"]
    tasks.transition(db, task_id, "COMPLETED", actor, "verified and approved", force=True)
    db.event(actor, "task.approved", task_id, {"sha": (sha or "")[:10]})
    _route_dependents(db, t, actor)
    w = db.one("SELECT * FROM workers WHERE id=?", (owner,)) if owner else None
    if w and w["state"] not in workers.TERMINAL:
        workers.terminate(db, owner, actor, "task verified and approved")
    return f"{task_id} approved at {(sha or 'n/a')[:10]}"


def _route_dependents(db: DB, t, actor: str) -> None:
    """Tell dependents that their upstream is done, with the producer's handoff (the 3D viewer -> frontend case)."""
    files = changed_files(db, t)
    for d in db.q("SELECT t.* FROM task_deps x JOIN tasks t ON t.id=x.task_id WHERE x.dep_id=?", (t["id"],)):
        msg = f"upstream task {t['id']} '{t['title']}' is COMPLETED. Handoff: {t['handoff'] or '(none)'} Files: {', '.join(files[:15]) or '-'}"
        to = d["session_id"] or (f"agent:{d['agent']}" if d["agent"] else "role:orchestrator")
        bus.send(db, actor, to, "dependency_ready", msg, d["id"], 1, requires_ack=bool(d["session_id"]), dedupe_key=f"depready-{t['id']}-{d['id']}")


def upstream_handoffs(db: DB, task_id: str) -> str:
    rows = db.q("SELECT t.* FROM task_deps x JOIN tasks t ON t.id=x.dep_id WHERE x.task_id=? AND t.status='COMPLETED'", (task_id,))
    return "\n".join(f"- {r['id']} {r['title']}: {r['handoff'] or '(no handoff note)'} (integration branch: mesh/integration)" for r in rows)


def reject(db: DB, task_id: str, actor: str, reason: str) -> str:
    t = tasks.get(db, task_id)
    if t["status"] != "REVIEW":
        raise MeshError(f"task is {t['status']}; only REVIEW tasks can be rejected")
    owner = t["session_id"]
    tasks.transition(db, task_id, "NEEDS_REWORK", actor, f"rejected: {reason}", force=True)
    db.event(actor, "task.rejected", task_id, {"reason": reason[:200]})
    if owner:
        bus.send(db, actor, owner, "review", f"REJECTED: {reason}", task_id, 1, requires_ack=True)
        w = db.one("SELECT * FROM workers WHERE id=?", (owner,))
        if w and w["state"] in ("COMPLETED", "FAILED", "REVIEWING") and not workers.pid_alive(w["runner_pid"], w["runner_start"]):
            workers.restart(db, owner, actor, None)  # message above is delivered via the resume brief
            return f"{task_id} sent back to {owner}; worker restarted with your feedback"
    return f"{task_id} marked NEEDS_REWORK"


def diff(db: DB, task_id: str) -> str:
    t = tasks.get(db, task_id)
    if not t["branch"]:
        return "(task has no branch)"
    rng = f"{base_branch(db)}...{t['branch']}"
    stat = git(db.paths.root, "diff", "--stat", rng, check=False).stdout
    body = git(db.paths.root, "diff", rng, check=False).stdout
    return stat + "\n" + (body[:20000] + ("\n…[truncated]" if len(body) > 20000 else ""))


def core_is_git(db: DB) -> bool:
    return core_is_git_root(db.paths.root)
