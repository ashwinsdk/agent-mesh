"""SQLite state store, project-root discovery, atomic file writes, audit events."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, cli TEXT NOT NULL, agent TEXT, native_id TEXT, pid INTEGER, host TEXT,
  model TEXT, task_id TEXT, started REAL NOT NULL, last_seen REAL NOT NULL, ended REAL, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, parent_id TEXT, kind TEXT NOT NULL DEFAULT 'task',
  title TEXT NOT NULL, description TEXT DEFAULT '', priority INTEGER DEFAULT 2,
  status TEXT NOT NULL DEFAULT 'BACKLOG', agent TEXT, session_id TEXT, cli TEXT,
  files TEXT DEFAULT '[]', acceptance TEXT DEFAULT '', created REAL NOT NULL, started REAL,
  last_activity REAL, completed REAL, checkpoint_id TEXT, branch TEXT, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS task_deps(task_id TEXT NOT NULL, dep_id TEXT NOT NULL, PRIMARY KEY(task_id, dep_id));
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL, ts REAL NOT NULL, task_id TEXT,
  type TEXT NOT NULL, priority INTEGER DEFAULT 2, content TEXT NOT NULL, dedupe_key TEXT UNIQUE, ack_by TEXT DEFAULT '[]', acked INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS msg_rcpt ON messages(recipient, acked);
CREATE TABLE IF NOT EXISTS locks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL, owner TEXT NOT NULL, task_id TEXT,
  acquired REAL NOT NULL, heartbeat REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints(
  id TEXT PRIMARY KEY, task_id TEXT, session_id TEXT, cli TEXT, path TEXT NOT NULL, sha256 TEXT NOT NULL,
  created REAL NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT, task_id TEXT, agent TEXT, cli TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_create INTEGER,
  context_tokens INTEGER, context_size INTEGER, context_pct REAL, cost_usd REAL,
  five_hour_pct REAL, five_hour_reset REAL, seven_day_pct REAL, seven_day_reset REAL,
  source TEXT NOT NULL DEFAULT 'unavailable', tool_calls INTEGER, requests INTEGER);
CREATE INDEX IF NOT EXISTS usage_sess ON usage(session_id, ts);
CREATE TABLE IF NOT EXISTS ratelimits(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT, cli TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT, type TEXT NOT NULL, task_id TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS memory(
  key TEXT PRIMARY KEY, version INTEGER NOT NULL, sha256 TEXT NOT NULL, author TEXT, ts REAL NOT NULL);
"""


class MeshError(Exception):
    pass


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def find_root(start: str | os.PathLike | None = None) -> Path:
    env = os.environ.get("MESH_ROOT")
    if env:
        return Path(env).resolve()
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / ".mesh").is_dir():
            return d
    raise MeshError("not inside a mesh project (no .mesh/ found); run `mesh init`")


class Paths:
    def __init__(self, root: Path):
        self.root = root
        self.mesh = root / ".mesh"
        self.runtime = self.mesh / "runtime"
        self.db = self.runtime / "mesh.db"
        self.memory = self.mesh / "memory"
        self.agents = self.mesh / "agents"
        self.checkpoints = self.runtime / "checkpoints"
        self.backups = self.runtime / "backups"
        self.logs = self.runtime / "logs"
        self.worktrees = self.runtime / "worktrees"
        self.config = self.mesh / "mesh.toml"


def atomic_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, mode) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


class DB:
    def __init__(self, paths: Paths, busy_ms: int = 30000):
        self.paths = paths
        paths.runtime.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(paths.db, timeout=busy_ms / 1000, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(f"PRAGMA busy_timeout={busy_ms}")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO meta VALUES('schema_version', '1')")
        from .migrate import migrate
        migrate(self.conn, SCHEMA_VERSION)

    @contextlib.contextmanager
    def tx(self):
        """Write transaction taking the write lock up front (no lost-update races)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def q(self, sql: str, args: tuple = ()):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        return self.conn.execute(sql, args).fetchone()

    def event(self, actor: str | None, type_: str, task_id: str | None = None, data: dict | None = None, conn=None):
        (conn or self.conn).execute(
            "INSERT INTO events(ts,actor,type,task_id,data) VALUES(?,?,?,?,?)",
            (now(), actor, type_, task_id, json.dumps(data or {}, default=str)),
        )

    def integrity(self) -> str:
        return self.one("PRAGMA integrity_check")[0]

    def close(self):
        self.conn.close()


def open_db(start=None) -> DB:
    return DB(Paths(find_root(start)))
