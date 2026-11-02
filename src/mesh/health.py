"""Project health checks shared by `mesh doctor` and the TUI banner. Returns [(level, text)] with level ok|warn|bad."""
from __future__ import annotations

import re

from . import __version__, core, workers
from .core import load_config
from .db import DB


def _vendored_version(db: DB) -> str | None:
    p = db.paths.mesh / "lib" / "mesh" / "__init__.py"
    if not p.exists():
        return None
    m = re.search(r'__version__\s*=\s*"([^"]+)"', p.read_text())
    return m.group(1) if m else None


def checks(db: DB) -> list[tuple[str, str]]:
    out = []
    cfg = load_config(db.paths)
    sv = int(db.one("SELECT value FROM meta WHERE key='schema_version'")[0])
    out.append(("ok", f"mesh {__version__}, database schema v{sv}"))
    vv = _vendored_version(db)
    here = (db.paths.mesh / "lib" / "mesh").resolve()
    from pathlib import Path
    running_vendored = Path(__file__).resolve().parent == here
    if vv and vv != __version__ and not running_vendored:
        out.append(("warn", f"version skew: this tool is {__version__} but the project's copy (.mesh/lib) is {vv}. "
                            "Workers and hooks use the project copy. Fix: `mesh upgrade && mesh sync && mesh commit-config`"))
    if running_vendored:
        out.append(("warn", "running the project's own copy of mesh; for the full app use the globally installed `mesh`"))
    if core.is_git(db.paths.root):
        r = core.git(db.paths.root, "status", "--porcelain", "--", ".mesh", "AGENTS.md", "CLAUDE.md", ".claude", ".github", ".agents", check=False)
        dirty = [l[3:] for l in r.stdout.splitlines() if ".mesh/runtime" not in l]
        if dirty:
            out.append(("warn", f"{len(dirty)} mesh-generated file(s) are modified/uncommitted (workers branch from the last commit): "
                                f"{', '.join(dirty[:4])}{'…' if len(dirty) > 4 else ''}. Fix: `mesh commit-config`"))
        try:
            workers.check_committed(db, cfg)
        except Exception as e:  # MeshError
            out.append(("bad", str(e)))
    else:
        out.append(("bad", "not a git repository: worktree isolation and integration need git"))
    a = cfg["autonomy"]
    out.append(("ok", f"autonomy accepted by {a['accepted_by']}") if a["accepted_at"] else ("warn", "autonomy policy not accepted yet (the app will ask)"))
    stale = db.one("SELECT COUNT(*) FROM sessions WHERE status='stale'")[0]
    if stale:
        out.append(("warn", f"{stale} stale session(s) hidden from the dashboard (silent > {cfg['sessions']['stale_after']}s)"))
    orchs = db.one("SELECT COUNT(*) FROM sessions WHERE role='orchestrator' AND status='active'")[0]
    if orchs > 1:
        out.append(("bad", f"{orchs} active orchestrator sessions (expected at most 1)"))
    try:
        from . import conductor
        st = conductor.status(db.paths)
        out.append(("ok", f"conductor running (pid {st['pid']})") if st["running"] else ("warn", "conductor is not running (the app starts it)"))
    except Exception:
        pass
    return out
