"""Path/glob locks with TTL, heartbeat, overlap detection and stale recovery."""
from __future__ import annotations

import fnmatch
import os
import socket

from .db import DB, MeshError, now


def norm(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/") if p not in ("", "/") else p


def _static_prefix(p: str) -> str:
    out = []
    for part in norm(p).split("/"):
        if any(c in part for c in "*?["):
            break
        out.append(part)
    return "/".join(out)


def overlaps(a: str, b: str) -> bool:
    """Conservative overlap test: true if the two patterns could cover a common path."""
    a, b = norm(a), norm(b)
    if a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    pa, pb = _static_prefix(a), _static_prefix(b)
    # A literal path with no glob is a directory lock if the other is inside it.
    def inside(x, y):  # x is under dir y
        return y == "" or x == y or x.startswith(y + "/")
    a_glob, b_glob = pa != a, pb != b
    if not a_glob and inside(b, a):  # a is a literal (file or dir)
        return True if not b_glob or inside(pb, a) else False
    if not b_glob and inside(a, b):
        return True if not a_glob or inside(pa, b) else False
    # both globs: overlap if one static prefix contains the other
    return inside(pa, pb) or inside(pb, pa)


def _pid_alive(pid: int, host: str) -> bool:
    if host != socket.gethostname():
        return True  # cannot tell; rely on TTL
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def expire_stale(conn, ts: float | None = None) -> int:
    ts = ts or now()
    cur = conn.execute("DELETE FROM locks WHERE expires < ?", (ts,))
    return cur.rowcount


def conflicts(conn, patterns: list[str], owner: str) -> list[dict]:
    expire_stale(conn)
    out = []
    for row in conn.execute("SELECT * FROM locks WHERE owner != ?", (owner,)).fetchall():
        for p in patterns:
            if overlaps(p, row["pattern"]):
                out.append({"pattern": p, "held": row["pattern"], "owner": row["owner"], "task": row["task_id"]})
    return out


def acquire(db: DB, owner: str, patterns: list[str], task_id: str | None = None, ttl: float = 900, conn=None):
    """All-or-nothing acquire. Returns list of conflicts (empty on success)."""
    patterns = [norm(p) for p in patterns if p.strip()]

    def _do(c):
        bad = conflicts(c, patterns, owner)
        if bad:
            return bad
        t = now()
        for p in patterns:
            row = c.execute("SELECT id FROM locks WHERE owner=? AND pattern=?", (owner, p)).fetchone()
            if row:
                c.execute("UPDATE locks SET heartbeat=?, expires=?, task_id=? WHERE id=?", (t, t + ttl, task_id, row["id"]))
            else:
                c.execute("INSERT INTO locks(pattern,owner,task_id,acquired,heartbeat,expires) VALUES(?,?,?,?,?,?)",
                          (p, owner, task_id, t, t, t + ttl))
        db.event(owner, "lock.acquire", task_id, {"patterns": patterns}, conn=c)
        return []

    if conn is not None:
        return _do(conn)
    with db.tx() as c:
        return _do(c)


def release(db: DB, owner: str, patterns: list[str] | None = None, task_id: str | None = None, conn=None) -> int:
    def _do(c):
        if patterns:
            n = 0
            for p in patterns:
                n += c.execute("DELETE FROM locks WHERE owner=? AND pattern=?", (owner, norm(p))).rowcount
        elif task_id:
            n = c.execute("DELETE FROM locks WHERE owner=? AND task_id=?", (owner, task_id)).rowcount
        else:
            n = c.execute("DELETE FROM locks WHERE owner=?", (owner,)).rowcount
        if n:
            db.event(owner, "lock.release", task_id, {"count": n}, conn=c)
        return n

    if conn is not None:
        return _do(conn)
    with db.tx() as c:
        return _do(c)


def heartbeat(db: DB, owner: str, ttl: float = 900) -> int:
    t = now()
    with db.tx() as c:
        return c.execute("UPDATE locks SET heartbeat=?, expires=? WHERE owner=?", (t, t + ttl, owner)).rowcount


def check_path(db: DB, owner: str, path: str) -> dict | None:
    """Is `path` locked by someone other than owner? Returns the blocking lock or None."""
    path = norm(path)
    with db.tx() as c:
        expire_stale(c)
    for row in db.q("SELECT * FROM locks WHERE owner != ?", (owner,)):
        pat = row["pattern"]
        if fnmatch.fnmatch(path, pat) or path == pat or path.startswith(pat + "/"):
            return dict(row)
    return None


def recover_dead(db: DB) -> int:
    """Drop locks whose owning session is ended or whose local pid is gone."""
    n = 0
    with db.tx() as c:
        n += expire_stale(c)
        for row in c.execute("SELECT DISTINCT owner FROM locks").fetchall():
            s = c.execute("SELECT * FROM sessions WHERE id=?", (row["owner"],)).fetchone()
            if s is None:
                continue
            if s["status"] != "active" or (s["pid"] and not _pid_alive(s["pid"], s["host"] or "")):
                n += c.execute("DELETE FROM locks WHERE owner=?", (row["owner"],)).rowcount
                db.event("mesh", "lock.recovered", None, {"owner": row["owner"]}, conn=c)
    return n
