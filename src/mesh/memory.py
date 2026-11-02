"""Layered shared memory: facts, ADRs, lessons; versioned, hash-guarded, secret-screened."""
from __future__ import annotations

import datetime as dt
import fcntl
import re
import time
from pathlib import Path

from .db import DB, MeshError, atomic_write, now, sha256
from .safety import scan_secrets

BANNER = "<!-- Memory may be stale: verify against source code/config before relying on it. -->\n"
APPEND_FILES = {"lessons": "lessons.md", "rejected": "rejected.md", "debt": "debt.md"}


def _today() -> str:
    return dt.date.today().isoformat()


def _file(db: DB, key: str) -> Path:
    if "/" in key or ".." in key:
        raise MeshError("bad memory key")
    return db.paths.memory / (key if key.endswith(".md") else key + ".md")


def _backup(db: DB, p: Path) -> None:
    if p.exists():
        d = db.paths.backups / "memory"
        d.mkdir(parents=True, exist_ok=True)
        atomic_write(d / f"{p.stem}-{int(time.time() * 1000)}.md", p.read_text())


class _flock:
    def __init__(self, db: DB):
        db.paths.runtime.mkdir(parents=True, exist_ok=True)
        self.f = open(db.paths.runtime / "memory.lock", "w")

    def __enter__(self):
        fcntl.flock(self.f, fcntl.LOCK_EX)

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def read(db: DB, key: str) -> tuple[str, str]:
    p = _file(db, key)
    text = p.read_text() if p.exists() else ""
    return text, sha256(text)


def _guard(text: str) -> None:
    hits = scan_secrets(text)
    if hits:
        raise MeshError(f"refusing to store likely secrets in memory ({', '.join(sorted(set(hits)))})")


def commit(db: DB, key: str, content: str, author: str, expected_sha: str) -> int:
    """Replace a memory file. Fails if the file changed since `expected_sha` was read (optimistic lock)."""
    _guard(content)
    p = _file(db, key)
    with _flock(db):
        _, cur = read(db, key)
        if cur != expected_sha:
            raise MeshError(f"memory '{key}' changed since you read it (have {expected_sha[:8]}, now {cur[:8]}); re-read and retry")
        _backup(db, p)
        atomic_write(p, content)
        with db.tx() as c:
            row = c.execute("SELECT version FROM memory WHERE key=?", (key,)).fetchone()
            v = (row["version"] + 1) if row else 1
            c.execute("INSERT INTO memory(key,version,sha256,author,ts) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET version=?, sha256=?, author=?, ts=?",
                      (key, v, sha256(content), author, now(), v, sha256(content), author, now()))
            db.event(author, "memory.commit", None, {"key": key, "version": v}, conn=c)
    return v


def add_fact(db: DB, text: str, author: str, status: str = "assumed", source: str = "") -> None:
    if status not in ("verified", "assumed"):
        raise MeshError("status must be verified|assumed")
    _guard(text)
    line = f"- [{status} | {_today()} | {author}] {text.strip()}" + (f" (source: {source})" if source else "")
    _append(db, "project", "## Facts", line, author)


def add_entry(db: DB, kind: str, text: str, author: str) -> None:
    if kind not in APPEND_FILES:
        raise MeshError(f"kind must be one of {sorted(APPEND_FILES)}")
    _guard(text)
    p = db.paths.memory / APPEND_FILES[kind]
    with _flock(db):
        _backup(db, p)
        cur = p.read_text() if p.exists() else f"# {kind.title()}\n\n"
        atomic_write(p, cur.rstrip("\n") + f"\n- [{_today()} | {author}] {text.strip()}\n")
        db.event(author, f"memory.{kind}", None, {"len": len(text)})


def _append(db: DB, key: str, heading: str, line: str, author: str) -> None:
    p = _file(db, key)
    with _flock(db):
        cur = p.read_text() if p.exists() else f"# Project memory\n\n{heading}\n"
        if heading not in cur:
            cur = cur.rstrip("\n") + f"\n\n{heading}\n"
        _backup(db, p)
        atomic_write(p, cur.rstrip("\n") + "\n" + line + "\n")
        db.event(author, "memory.fact", None, {"key": key})


def new_adr(db: DB, title: str, context: str, decision: str, author: str, consequences: str = "") -> str:
    _guard(context + decision + consequences)
    d = db.paths.memory / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    with _flock(db):
        n = 1 + max([int(m.group(1)) for f in d.glob("ADR-*.md") if (m := re.match(r"ADR-(\d+)", f.name))] or [0])
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
        p = d / f"ADR-{n:04d}-{slug}.md"
        atomic_write(p, f"# ADR-{n:04d}: {title}\n\n- Date: {_today()}\n- Author: {author}\n- Status: accepted\n\n"
                        f"## Context\n{context}\n\n## Decision\n{decision}\n\n## Consequences\n{consequences}\n")
        db.event(author, "memory.adr", None, {"adr": p.name})
    return p.name


def history(db: DB, key: str) -> list[Path]:
    d = db.paths.backups / "memory"
    return sorted(d.glob(f"{_file(db, key).stem}-*.md")) if d.exists() else []


def restore(db: DB, key: str, snapshot: Path, author: str) -> None:
    _, cur = read(db, key)
    commit(db, key, snapshot.read_text(), author, cur)


def stale_report(db: DB, max_age_days: int = 60) -> list[str]:
    """Flag facts that are old, still only 'assumed', or reference files that no longer exist."""
    out = []
    p = db.paths.memory / "project.md"
    if not p.exists():
        return out
    today = dt.date.today()
    for ln in p.read_text().splitlines():
        m = re.match(r"- \[(verified|assumed) \| (\d{4}-\d\d-\d\d) \| ([^\]]+)\] (.*)", ln)
        if not m:
            continue
        age = (today - dt.date.fromisoformat(m.group(2))).days
        if age > max_age_days:
            out.append(f"STALE ({age}d): {m.group(4)[:80]}")
        elif m.group(1) == "assumed" and age > 14:
            out.append(f"UNVERIFIED ({age}d): {m.group(4)[:80]}")
        for path in re.findall(r"`([\w./\-]+\.\w+)`", m.group(4)):
            if "/" in path and not (db.paths.root / path).exists():
                out.append(f"MISSING FILE `{path}` referenced by: {m.group(4)[:60]}")
    return out


def working_memory(db: DB) -> str:
    """Layer B: regenerated from the DB, never hand-edited."""
    def rows(sql, a=()):
        return db.q(sql, a)
    lines = [f"# Shared working memory (generated {time.strftime('%Y-%m-%d %H:%M:%S')})", ""]
    for title, st in [("In progress", ("ASSIGNED", "IN_PROGRESS", "REVIEW", "NEEDS_REWORK")),
                      ("Ready", ("READY",)), ("Blocked/Paused", ("BLOCKED", "PAUSED", "FAILED"))]:
        ph = ",".join("?" * len(st))
        rs = rows(f"SELECT * FROM tasks WHERE status IN ({ph}) AND kind!='project' ORDER BY priority", st)
        lines.append(f"## {title} ({len(rs)})")
        lines += [f"- {r['id']} [{r['status']}] {r['title']} — owner={r['session_id'] or '-'} agent={r['agent'] or '-'}" for r in rs]
        lines.append("")
    rs = rows("SELECT * FROM tasks WHERE status='COMPLETED' ORDER BY completed DESC LIMIT 10")
    lines.append("## Recently completed")
    lines += [f"- {r['id']} {r['title']}" for r in rs]
    lines += ["", "## File locks"]
    lines += [f"- {r['pattern']} held by {r['owner']} (task {r['task_id'] or '-'})" for r in rows("SELECT * FROM locks")]
    return "\n".join(lines) + "\n"
