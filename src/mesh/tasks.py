"""Hierarchical tasks, validated state machine, atomic claim, dependencies, abandonment."""
from __future__ import annotations

import json

from . import locks as L
from .db import DB, MeshError, new_id, now

STATES = ["BACKLOG", "READY", "ASSIGNED", "IN_PROGRESS", "REVIEW", "COMPLETED",
          "BLOCKED", "PAUSED", "FAILED", "CANCELLED", "NEEDS_REWORK"]
KINDS = ["project", "epic", "task", "subtask", "step"]
TRANSITIONS = {
    "BACKLOG": {"READY", "CANCELLED"},
    "READY": {"ASSIGNED", "BLOCKED", "CANCELLED", "BACKLOG"},
    "ASSIGNED": {"IN_PROGRESS", "READY", "PAUSED", "CANCELLED", "BLOCKED"},
    "IN_PROGRESS": {"REVIEW", "BLOCKED", "PAUSED", "FAILED", "READY", "CANCELLED"},
    "REVIEW": {"COMPLETED", "NEEDS_REWORK", "IN_PROGRESS"},
    "NEEDS_REWORK": {"ASSIGNED", "IN_PROGRESS", "READY", "CANCELLED"},
    "BLOCKED": {"READY", "IN_PROGRESS", "CANCELLED"},
    "PAUSED": {"READY", "IN_PROGRESS", "CANCELLED"},
    "FAILED": {"READY", "NEEDS_REWORK", "CANCELLED"},
    "COMPLETED": {"NEEDS_REWORK"},
    "CANCELLED": {"BACKLOG"},
}
# states in which the task is "held" by an agent/session
HELD = ("ASSIGNED", "IN_PROGRESS", "REVIEW", "NEEDS_REWORK", "PAUSED", "BLOCKED")


def create(db: DB, title: str, description: str = "", parent: str | None = None, kind: str | None = None,
           priority: int = 2, deps: list[str] | None = None, files: list[str] | None = None,
           acceptance: str = "", agent: str | None = None, backlog: bool = False, actor: str = "human",
           task_id: str | None = None) -> str:
    deps = deps or []
    with db.tx() as c:
        if parent:
            prow = c.execute("SELECT kind FROM tasks WHERE id=?", (parent,)).fetchone()
            if not prow:
                raise MeshError(f"unknown parent {parent}")
            kind = kind or KINDS[min(KINDS.index(prow["kind"]) + 1, len(KINDS) - 1)]
            if KINDS.index(kind) <= KINDS.index(prow["kind"]):
                raise MeshError(f"{kind} cannot be a child of {prow['kind']}")
        kind = kind or "task"
        if kind not in KINDS:
            raise MeshError(f"bad kind {kind}")
        for d in deps:
            if not c.execute("SELECT 1 FROM tasks WHERE id=?", (d,)).fetchone():
                raise MeshError(f"unknown dependency {d}")
        tid = task_id or new_id("T")
        if c.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
            raise MeshError(f"task {tid} exists")
        status = "BACKLOG" if (backlog or deps) else "READY"
        t = now()
        c.execute("INSERT INTO tasks(id,parent_id,kind,title,description,priority,status,agent,files,acceptance,created,last_activity)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, parent, kind, title, description, priority, status, agent, json.dumps(files or []), acceptance, t, t))
        for d in deps:
            if _would_cycle(c, tid, d):
                raise MeshError("dependency cycle")
            c.execute("INSERT INTO task_deps VALUES(?,?)", (tid, d))
        db.event(actor, "task.create", tid, {"title": title, "status": status}, conn=c)
    return tid


def _would_cycle(c, tid: str, dep: str) -> bool:
    seen, stack = set(), [dep]
    while stack:
        x = stack.pop()
        if x == tid:
            return True
        if x in seen:
            continue
        seen.add(x)
        stack += [r[0] for r in c.execute("SELECT dep_id FROM task_deps WHERE task_id=?", (x,)).fetchall()]
    return False


def add_dep(db: DB, tid: str, dep: str) -> None:
    with db.tx() as c:
        for x in (tid, dep):
            if not c.execute("SELECT 1 FROM tasks WHERE id=?", (x,)).fetchone():
                raise MeshError(f"unknown task {x}")
        if tid == dep or _would_cycle(c, tid, dep):
            raise MeshError("dependency cycle")
        c.execute("INSERT OR IGNORE INTO task_deps VALUES(?,?)", (tid, dep))
        c.execute("UPDATE tasks SET status='BACKLOG' WHERE id=? AND status='READY'", (tid,))


def get(db: DB, tid: str):
    row = db.one("SELECT * FROM tasks WHERE id=?", (tid,))
    if not row:
        raise MeshError(f"unknown task {tid}")
    return row


def deps_done(c, tid: str) -> bool:
    n = c.execute("SELECT COUNT(*) FROM task_deps d JOIN tasks t ON t.id=d.dep_id "
                  "WHERE d.task_id=? AND t.status!='COMPLETED'", (tid,)).fetchone()[0]
    return n == 0


def refresh_ready(db: DB, conn=None) -> list[str]:
    """Promote BACKLOG tasks whose dependencies are all COMPLETED."""
    def _do(c):
        out = []
        for r in c.execute("SELECT id FROM tasks WHERE status='BACKLOG' AND id IN (SELECT task_id FROM task_deps)").fetchall():
            if deps_done(c, r["id"]):
                c.execute("UPDATE tasks SET status='READY', last_activity=? WHERE id=?", (now(), r["id"]))
                db.event("mesh", "task.ready", r["id"], conn=c)
                out.append(r["id"])
        return out
    if conn is not None:
        return _do(conn)
    with db.tx() as c:
        return _do(c)


def ready(db: DB, agent: str | None = None) -> list:
    """Tasks that can be started now, ordered by priority; independent ones can run concurrently."""
    refresh_ready(db)
    rows = db.q("SELECT * FROM tasks WHERE status='READY' AND session_id IS NULL AND kind!='project' ORDER BY priority, created")
    if agent:
        rows = [r for r in rows if r["agent"] in (None, agent)]
    return rows


def claim(db: DB, tid: str, session: str, agent: str | None = None, cli: str | None = None,
          lock_ttl: float = 900) -> dict:
    """Atomically claim a READY task and lock its files. Exactly one concurrent claimer wins."""
    conflict_msg = None
    with db.tx() as c:
        refresh_ready(db, conn=c)
        row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise MeshError(f"unknown task {tid}")
        if row["session_id"] and row["session_id"] != session and row["status"] in HELD:
            raise MeshError(f"task {tid} already owned by {row['session_id']} ({row['status']})")
        if row["status"] != "READY":
            raise MeshError(f"task {tid} is {row['status']}, not READY")
        if not deps_done(c, tid):
            raise MeshError(f"task {tid} has unfinished dependencies")
        files = json.loads(row["files"] or "[]")
        if files:
            bad = L.acquire(db, session, files, tid, lock_ttl, conn=c)
            if bad:
                # audit the collision; commit the event, then raise after the tx closes
                db.event(session, "task.claim_conflict", tid, {"conflicts": bad}, conn=c)
                conflict_msg = "file ownership conflict: " + "; ".join(
                    f"{b['pattern']} overlaps {b['held']} held by {b['owner']}" for b in bad)
        n = 0 if conflict_msg else c.execute("UPDATE tasks SET status='ASSIGNED', session_id=?, agent=COALESCE(?,agent), cli=?, last_activity=? "
                      "WHERE id=? AND status='READY' AND session_id IS NULL",
                      (session, agent, cli, now(), tid)).rowcount
        if not conflict_msg:
            if n != 1:
                raise MeshError(f"task {tid} was claimed by someone else")
            c.execute("UPDATE sessions SET task_id=? WHERE id=?", (tid, session))
            db.event(session, "task.claim", tid, {"agent": agent, "cli": cli}, conn=c)
    if conflict_msg:
        raise MeshError(conflict_msg)
    return dict(get(db, tid))


def transition(db: DB, tid: str, new: str, actor: str, note: str = "", force: bool = False) -> None:
    if new not in STATES:
        raise MeshError(f"bad state {new}")
    with db.tx() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise MeshError(f"unknown task {tid}")
        old = row["status"]
        if new not in TRANSITIONS.get(old, set()) and not force:
            raise MeshError(f"illegal transition {old} -> {new}")
        if row["session_id"] and row["session_id"] != actor and actor not in ("human", "mesh") and old in HELD and not force:
            raise MeshError(f"task {tid} is owned by {row['session_id']}")
        t = now()
        sets, args = ["status=?", "last_activity=?"], [new, t]
        if new == "IN_PROGRESS" and not row["started"]:
            sets.append("started=?"); args.append(t)
        if new == "COMPLETED":
            sets.append("completed=?"); args.append(t)
        if new in ("READY", "CANCELLED", "BACKLOG", "COMPLETED", "FAILED"):
            if new in ("READY", "CANCELLED", "BACKLOG"):
                sets.append("session_id=NULL")
            L.release(db, row["session_id"] or actor, task_id=tid, conn=c)
        if note:
            sets.append("notes=notes||?"); args.append(f"\n[{t:.0f}] {actor}: {note}")
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*args, tid))
        db.event(actor, "task.transition", tid, {"from": old, "to": new, "note": note}, conn=c)
        if new == "COMPLETED":
            refresh_ready(db, conn=c)


def touch(db: DB, tid: str, actor: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE tasks SET last_activity=? WHERE id=?", (now(), tid))


def abandoned(db: DB, stale_after: float = 1800) -> list:
    """Held tasks whose owning session is ended or silent for stale_after seconds."""
    t = now()
    out = []
    for r in db.q("SELECT t.*, s.last_seen AS s_seen, s.status AS s_status FROM tasks t "
                  "LEFT JOIN sessions s ON s.id=t.session_id WHERE t.status IN ('ASSIGNED','IN_PROGRESS') AND t.session_id IS NOT NULL"):
        seen = max(r["s_seen"] or 0, r["last_activity"] or 0)
        if r["s_status"] != "active" or t - seen > stale_after:
            out.append(r)
    return out


def reassign(db: DB, tid: str, actor: str = "mesh", release_to_pool: bool = True) -> None:
    """Return a held task to READY (keeping notes/checkpoint reference) so another session can claim it."""
    with db.tx() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if not row:
            raise MeshError(f"unknown task {tid}")
        old_owner = row["session_id"]
        L.release(db, old_owner or actor, task_id=tid, conn=c)
        c.execute("UPDATE tasks SET status='READY', session_id=NULL, last_activity=?, notes=notes||? WHERE id=?",
                  (now(), f"\n[reassign] released from {old_owner}", tid))
        db.event(actor, "task.reassign", tid, {"from": old_owner}, conn=c)


def reap(db: DB, stale_after: float = 1800) -> list[str]:
    ids = []
    for r in abandoned(db, stale_after):
        reassign(db, r["id"])
        ids.append(r["id"])
    return ids


def resume(db: DB, tid: str, session: str, stale_after: float = 1800, lock_ttl: float = 900,
           allow_ready: bool = True) -> dict:
    """Take over a task whose owner is gone (ended/crashed/silent). Refuses to steal from a live session."""
    row = get(db, tid)
    owner = row["session_id"]
    if owner == session:
        return dict(row)
    if owner:
        if not any(r["id"] == tid for r in abandoned(db, stale_after)):
            raise MeshError(f"task {tid} is actively owned by {owner}; refusing to take over (ask it to `mesh task release`)")
        reassign(db, tid, session)
    elif row["status"] not in ("READY", "NEEDS_REWORK", "PAUSED", "BLOCKED", "FAILED") or (row["status"] == "READY" and not allow_ready):
        raise MeshError(f"task {tid} is {row['status']} and unowned; nothing to resume")
    row = get(db, tid)
    if row["status"] in ("PAUSED", "BLOCKED", "FAILED"):
        transition(db, tid, "READY", session)
    if row["status"] == "NEEDS_REWORK":
        # claim() requires READY; NEEDS_REWORK -> READY is allowed
        transition(db, tid, "READY", session)
    claim(db, tid, session, lock_ttl=lock_ttl)
    transition(db, tid, "IN_PROGRESS", session, f"resumed from {owner or 'pool'}")
    db.event(session, "task.resume", tid, {"from": owner})
    return dict(get(db, tid))


def children(db: DB, tid: str):
    return db.q("SELECT * FROM tasks WHERE parent_id=? ORDER BY created", (tid,))


def progress(db: DB, tid: str) -> float:
    kids = children(db, tid)
    if not kids:
        return 1.0 if get(db, tid)["status"] == "COMPLETED" else 0.0
    return sum(progress(db, k["id"]) for k in kids) / len(kids)
