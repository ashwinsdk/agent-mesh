"""Checkpoints (CLI-neutral JSON with integrity hash) and the resume brief."""
from __future__ import annotations

import json
from pathlib import Path

from . import bus, tasks
from .core import git_state
from .db import DB, MeshError, atomic_write, new_id, now, sha256
from .safety import redact

FIELDS = ["completed_steps", "current_stage", "decisions", "commands", "tests", "issues", "next_actions", "memory_refs"]


def _body_hash(body: dict) -> str:
    return sha256(json.dumps(body, sort_keys=True, default=str))


def create(db: DB, session: str, task_id: str | None, data: dict | None = None, reason: str = "manual",
           cli: str | None = None) -> str:
    data = data or {}
    sess = db.one("SELECT cli, agent FROM sessions WHERE id=?", (session,))
    task_row = tasks.get(db, task_id) if task_id else None
    body = {
        "task_id": task_id, "task_title": task_row["title"] if task_row else None,
        "task_status": task_row["status"] if task_row else None,
        "acceptance": task_row["acceptance"] if task_row else None,
        "files": json.loads(task_row["files"]) if task_row else [],
        "session": session, "cli": cli or (sess["cli"] if sess else None), "agent": sess["agent"] if sess else None,
        "reason": reason, "created": now(), "git": git_state(db.paths.root),
    }
    for f in FIELDS:
        v = data.get(f, [])
        body[f] = [redact(str(x)) for x in v] if isinstance(v, list) else redact(str(v))
    cid = new_id("CP")
    doc = {"body": body, "sha256": _body_hash(body)}
    p = db.paths.checkpoints / f"{cid}.json"
    atomic_write(p, json.dumps(doc, indent=2, default=str))
    with db.tx() as c:
        c.execute("INSERT INTO checkpoints(id,task_id,session_id,cli,path,sha256,created,reason) VALUES(?,?,?,?,?,?,?,?)",
                  (cid, task_id, session, body["cli"], str(p), doc["sha256"], body["created"], reason))
        if task_id:
            c.execute("UPDATE tasks SET checkpoint_id=?, last_activity=? WHERE id=?", (cid, now(), task_id))
        db.event(session, "checkpoint.create", task_id, {"id": cid, "reason": reason}, conn=c)
    return cid


def load(path: str | Path) -> dict | None:
    """Return the body if the file parses and its hash matches, else None."""
    try:
        doc = json.loads(Path(path).read_text())
        if doc["sha256"] == _body_hash(doc["body"]):
            return doc["body"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def latest_valid(db: DB, task_id: str | None = None) -> tuple[str, dict] | None:
    sql = "SELECT * FROM checkpoints " + ("WHERE task_id=? " if task_id else "") + "ORDER BY created DESC"
    for r in db.q(sql, (task_id,) if task_id else ()):
        body = load(r["path"])
        if body is not None:
            return r["id"], body
        db.event("mesh", "checkpoint.corrupt", r["task_id"], {"id": r["id"]})
    return None


def interrupted(db: DB, stale_after: float = 1800) -> list:
    return tasks.abandoned(db, stale_after)


def verify_against_repo(db: DB, body: dict) -> list[str]:
    """Compare checkpoint's recorded repo state with reality; never trust blindly."""
    notes = []
    cur = git_state(db.paths.root)
    old = body.get("git", {})
    if cur.get("git") and old.get("git"):
        if cur["head"] != old.get("head"):
            notes.append(f"HEAD moved since checkpoint ({(old.get('head') or '')[:8]} -> {(cur['head'] or '')[:8]}): re-read `git log` before continuing")
        if cur["branch"] != old.get("branch"):
            notes.append(f"branch differs (checkpoint {old.get('branch')}, now {cur['branch']})")
        gone = [f for f in old.get("dirty", []) if f not in cur["dirty"]]
        if gone:
            notes.append(f"files dirty at checkpoint but clean now (committed or reverted?): {', '.join(gone[:8])}")
        new = [f for f in cur["dirty"] if f not in old.get("dirty", [])]
        if new:
            notes.append(f"files changed after checkpoint: {', '.join(new[:8])}")
    return notes


def brief(db: DB, task_id: str | None, who: str, budget: int = 6000) -> str:
    """Continuation brief for any CLI. Works without the original conversation."""
    cp = latest_valid(db, task_id)
    out = ["# Mesh resume brief", "Facts below came from persisted state; verify against the repository before acting."]
    if task_id:
        t = tasks.get(db, task_id)
        out += [f"\n## Task {t['id']}: {t['title']} [{t['status']}]", t["description"] or "",
                f"Acceptance: {t['acceptance'] or '(none stated)'}", f"Files: {t['files']}",
                f"Previous owner: {t['session_id'] or '-'}  branch: {t['branch'] or '-'}"]
    if cp:
        cid, b = cp
        out.append(f"\n## Latest valid checkpoint {cid} (by {b.get('cli')}/{b.get('agent')}, reason={b.get('reason')})")
        for f in FIELDS:
            v = b.get(f)
            if v:
                out.append(f"### {f}\n" + ("\n".join(f"- {x}" for x in v) if isinstance(v, list) else str(v)))
        notes = verify_against_repo(db, b)
        if notes:
            out.append("### Repo drift warnings\n" + "\n".join(f"- {n}" for n in notes))
    else:
        out.append("\n## No valid checkpoint found — inspect `git status`, `git log`, and task notes; do not assume progress.")
    msgs = bus.inbox(db, who)
    if msgs:
        out.append("\n" + bus.render_untrusted(msgs[:10]))
    text = "\n".join(out)
    if len(text) > budget:
        text = text[:budget] + "\n…[brief truncated to budget; use `mesh checkpoint show` for full detail]"
    return text
