"""Runs (one project objective each), the command queue (single writer = conductor) and attention items."""
from __future__ import annotations

import json

from .db import DB, MeshError, now

TERMINAL = ("complete", "failed", "halted")
STATUSES = ("planning", "awaiting_approval", "executing", "paused", "integrating", "complete", "failed", "halted")


def active_run(db: DB):
    r = db.one("SELECT * FROM runs WHERE status NOT IN ('complete','failed','halted') ORDER BY id DESC LIMIT 1")
    return r


def latest_run(db: DB):
    return db.one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")


def create_run(db: DB, prompt: str, cli: str | None, actor: str = "human") -> int:
    with db.tx() as c:
        if c.execute("SELECT 1 FROM runs WHERE status NOT IN ('complete','failed','halted')").fetchone():
            raise MeshError("a run is already active; send your message as a directive instead, or /halt it first")
        t = now()
        c.execute("INSERT INTO runs(prompt,cli,status,created,updated) VALUES(?,?,?,?,?)", (prompt, cli, "planning", t, t))
        rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.event(actor, "run.created", None, {"run": rid, "cli": cli, "prompt": prompt[:200]}, conn=c)
    return rid


def set_status(db: DB, rid: int, status: str, note: str = "", error: str | None = None) -> None:
    assert status in STATUSES, status
    with db.tx() as c:
        old = c.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
        t = now()
        c.execute("UPDATE runs SET status=?, updated=?, ended=CASE WHEN ? IN ('complete','failed','halted') THEN ? ELSE ended END, "
                  "error=COALESCE(?, CASE WHEN ? IN ('executing','planning') THEN NULL ELSE error END) WHERE id=?", (status, t, status, t, error, status, rid))
        if old and old["status"] != status:
            db.event("conductor", "run.status", None, {"run": rid, "from": old["status"], "to": status, "note": note[:200]}, conn=c)


def enqueue(db: DB, kind: str, payload: dict | None = None, actor: str = "human") -> int:
    with db.tx() as c:
        c.execute("INSERT INTO commands(ts,kind,payload,actor) VALUES(?,?,?,?)", (now(), kind, json.dumps(payload or {}), actor))
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def pending_commands(db: DB):
    return db.q("SELECT * FROM commands WHERE status='pending' ORDER BY id")


def finish_command(db: DB, cid: int, ok: bool, result: str = "") -> None:
    with db.tx() as c:
        c.execute("UPDATE commands SET status=?, result=?, done=? WHERE id=?", ("done" if ok else "error", result[:500], now(), cid))


def add_attention(db: DB, run_id: int | None, kind: str, subject: str | None, title: str, detail: str = "", payload: dict | None = None) -> int | None:
    """Open an item that needs the user (or brain). Deduplicated per (kind, subject) while open. Returns id or None if duplicate."""
    with db.tx() as c:
        if c.execute("SELECT 1 FROM attention WHERE status='open' AND kind=? AND COALESCE(subject,'')=COALESCE(?,'')", (kind, subject)).fetchone():
            return None
        c.execute("INSERT INTO attention(ts,run_id,kind,subject,title,detail,payload) VALUES(?,?,?,?,?,?,?)",
                  (now(), run_id, kind, subject, title, detail[:2000], json.dumps(payload or {})))
        aid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.event("conductor", "attention.open", subject, {"id": aid, "kind": kind, "title": title[:120]}, conn=c)
    return aid


def resolve_attention(db: DB, aid: int, by: str = "human") -> None:
    with db.tx() as c:
        c.execute("UPDATE attention SET status='resolved', resolved_by=?, resolved=? WHERE id=?", (by, now(), aid))


def resolve_subject(db: DB, kind: str, subject: str, by: str = "conductor") -> None:
    with db.tx() as c:
        c.execute("UPDATE attention SET status='resolved', resolved_by=?, resolved=? WHERE status='open' AND kind=? AND subject=?", (by, now(), kind, subject))


def open_attention(db: DB):
    return db.q("SELECT * FROM attention WHERE status='open' ORDER BY id")
