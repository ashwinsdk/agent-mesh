"""Schema migrations. v1 -> v2 adds the worker/process model, protocol v2 messages, verification and approvals."""
from __future__ import annotations

V2_TABLES = """
CREATE TABLE IF NOT EXISTS workers(
  id TEXT PRIMARY KEY, agent TEXT, role TEXT DEFAULT 'worker', cli TEXT NOT NULL, model TEXT,
  native_session TEXT, runner_pid INTEGER, runner_start TEXT, pid INTEGER, pgid INTEGER,
  state TEXT NOT NULL DEFAULT 'INITIALISING', state_detail TEXT, depth INTEGER DEFAULT 0, parent_worker TEXT,
  restarts INTEGER DEFAULT 0, route_idx INTEGER DEFAULT 0, run_dir TEXT, worktree TEXT, task_id TEXT,
  started REAL NOT NULL, last_event REAL, last_output REAL, exit_code INTEGER, ended_reason TEXT, ended REAL,
  progress REAL, blocker TEXT, last_activity TEXT);
CREATE INDEX IF NOT EXISTS workers_state ON workers(state);
CREATE TABLE IF NOT EXISTS assignments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT NOT NULL, task_id TEXT NOT NULL,
  started REAL NOT NULL, ended REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS cursors(consumer TEXT PRIMARY KEY, seq INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS verifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, sha TEXT, ok INTEGER NOT NULL,
  weak INTEGER DEFAULT 0, detail TEXT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL, requested_by TEXT,
  payload TEXT, status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT, ts REAL NOT NULL, decided REAL);
"""

V4_TABLES = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, prompt TEXT NOT NULL, cli TEXT, status TEXT NOT NULL DEFAULT 'planning',
  summary TEXT, plan_json TEXT, error TEXT, brain_session TEXT, brain_turns INTEGER DEFAULT 0, plan_attempts INTEGER DEFAULT 0,
  created REAL NOT NULL, updated REAL NOT NULL, ended REAL, final_validation TEXT);
CREATE TABLE IF NOT EXISTS commands(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, payload TEXT DEFAULT '{}', actor TEXT,
  status TEXT NOT NULL DEFAULT 'pending', result TEXT, done REAL);
CREATE INDEX IF NOT EXISTS cmd_pending ON commands(status, id);
CREATE TABLE IF NOT EXISTS attention(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, run_id INTEGER, kind TEXT NOT NULL, subject TEXT,
  title TEXT NOT NULL, detail TEXT, payload TEXT, status TEXT NOT NULL DEFAULT 'open', resolved_by TEXT, resolved REAL);
CREATE INDEX IF NOT EXISTS att_open ON attention(status, kind, subject);
CREATE TABLE IF NOT EXISTS conductor(
  id INTEGER PRIMARY KEY CHECK (id=1), pid INTEGER, pid_start TEXT, started REAL, heartbeat REAL, state TEXT, run_id INTEGER, version TEXT)
"""

ADD_COLUMNS = {
    "sessions": [("role", "TEXT"), ("worker_id", "TEXT")],
    "tasks": [("verify_cmd", "TEXT DEFAULT ''"), ("risk", "TEXT DEFAULT 'low'"), ("review_required", "INTEGER DEFAULT 0"),
              ("submitted_sha", "TEXT"), ("eta_minutes", "INTEGER"), ("handoff", "TEXT DEFAULT ''")],
    "messages": [("correlation_id", "TEXT"), ("reply_to", "TEXT"), ("requires_ack", "INTEGER DEFAULT 0"), ("expires_at", "REAL"),
                 ("attempts", "INTEGER DEFAULT 0"), ("delivered_at", "REAL"), ("delivery_state", "TEXT DEFAULT 'pending'"),
                 ("last_attempt", "REAL")],
    "usage": [("worker_id", "TEXT"), ("billing_mode", "TEXT"), ("cost_source", "TEXT"), ("turn", "INTEGER")],
}


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(conn, target: int = 4) -> None:
    cur = int((conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() or ["1"])[0])
    if cur >= target:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])  # re-check under lock
        if cur < 2:
            for stmt in [s.strip() for s in V2_TABLES.split(";") if s.strip()]:
                conn.execute(stmt)
            for table, cols in ADD_COLUMNS.items():
                have = _cols(conn, table)
                for name, ddl in cols:
                    if name not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            conn.execute("CREATE INDEX IF NOT EXISTS msg_pending ON messages(requires_ack, acked, delivery_state)")
            conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        if cur < 3:
            # Copilot reports usage in nano-AIU (its billing unit). Stored as AIU, never converted to currency.
            if "ai_credits" not in _cols(conn, "usage"):
                conn.execute("ALTER TABLE usage ADD COLUMN ai_credits REAL")
            conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        if cur < 4:
            for stmt in [x.strip() for x in V4_TABLES.split(";") if x.strip()]:
                conn.execute(stmt)
            for col, ddl in (("run_id", "INTEGER"), ("prefer_cli", "TEXT")):
                if col not in _cols(conn, "tasks"):
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
            conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
