"""Event-driven supervision: cursor-based reads and a cheap blocking wait (no daemon, no busy scanning)."""
from __future__ import annotations

import json
import time

from . import bus
from .db import DB, now

# Bookkeeping events the orchestrator does not need to be woken for (still shown by `mesh tail --all` and the dashboard).
NOISY = {"tool", "session.resume", "session.start", "session.end", "msg.ack", "lock.acquire", "lock.release", "lock.recovered",
         "worker.runner_started", "worker.state", "worktree.create", "task.create", "task.ready", "task.claim", "task.resume",
         "checkpoint.create", "memory.fact", "memory.commit", "memory.adr", "memory.lessons", "memory.rejected", "memory.debt",
         "init", "worker.restart_requested", "autonomy.accepted"}
WAKE_TRANSITIONS = {"REVIEW", "BLOCKED", "FAILED", "COMPLETED", "NEEDS_REWORK", "PAUSED"}


def _targets(db: DB, consumer: str, actor: str) -> set[str]:
    t = {"*", actor, consumer}
    if consumer == "orchestrator":
        t.add("role:orchestrator")
    return t


def _wanted(db: DB, ev, include_noisy: bool, targets: set[str] | None, own: str | None) -> bool:
    if include_noisy:
        return True
    t = ev["type"]
    if own and ev["actor"] == own:
        return False  # your own actions are not news
    if t in NOISY:
        return False
    data = json.loads(ev["data"] or "{}")
    if t == "task.transition":
        return data.get("to") in WAKE_TRANSITIONS
    if t == "msg.send":
        return targets is None or data.get("to") in targets
    return True


def since(db: DB, seq: int, include_noisy: bool = False, limit: int = 200, targets: set[str] | None = None, own: str | None = None):
    rows = db.q("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (seq, 2000))
    out = [r for r in rows if _wanted(db, r, include_noisy, targets, own)]
    return out[:limit]


def max_seq(db: DB) -> int:
    return db.one("SELECT COALESCE(MAX(id),0) FROM events")[0]


def get_cursor(db: DB, consumer: str) -> int:
    r = db.one("SELECT seq FROM cursors WHERE consumer=?", (consumer,))
    return r["seq"] if r else 0


def set_cursor(db: DB, consumer: str, seq: int) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO cursors(consumer,seq) VALUES(?,?) ON CONFLICT(consumer) DO UPDATE SET seq=MAX(seq,excluded.seq)", (consumer, seq))


def wait(db: DB, consumer: str, timeout: float, include_noisy: bool = False, advance: bool = True, actor: str | None = None):
    """Block until at least one wanted event is newer than the consumer's cursor, or timeout. Returns the events.
    Sleeps on PRAGMA data_version (changes only when another connection commits): idle cost is a few cheap pragma reads/sec."""
    actor = actor or consumer
    targets = _targets(db, consumer, actor)
    end = time.time() + timeout
    last_sweep = 0.0
    pos = get_cursor(db, consumer)
    while True:
        rows = db.q("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 2000", (pos,))
        if rows:
            pos = rows[-1]["id"]
            wanted = [r for r in rows if _wanted(db, r, include_noisy, targets, actor)]
            if wanted:
                if advance:
                    set_cursor(db, consumer, pos)
                return wanted
            if advance:
                set_cursor(db, consumer, pos)  # skip past noise so it is not rescanned
            continue
        if time.time() - last_sweep > 5:
            bus.sweep(db)
            last_sweep = time.time()
            continue
        if time.time() >= end:
            return []
        v = db.one("PRAGMA data_version")[0]
        while time.time() < end and db.one("PRAGMA data_version")[0] == v:
            time.sleep(0.25)


def render(db: DB, rows) -> str:
    out = []
    for r in rows:
        data = json.loads(r["data"] or "{}")
        brief = ", ".join(f"{k}={str(v)[:70]}" for k, v in data.items() if v not in (None, "", [], {}))
        out.append(f"#{r['id']} {time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['type']} actor={r['actor'] or '-'} task={r['task_id'] or '-'} {brief}")
    return "\n".join(out)
