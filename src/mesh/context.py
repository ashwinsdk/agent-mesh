"""Task-scoped context briefs and threshold actions (warn -> checkpoint -> rollover prompt)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import bus, checkpoint, memory, tasks
from .core import load_config
from .db import DB, MeshError


def _score(text: str, terms: set[str]) -> int:
    words = set(re.findall(r"[a-z0-9_]{3,}", text.lower()))
    return len(words & terms)


def brief(db: DB, task_id: str, who: str, budget: int | None = None) -> str:
    """Hierarchical, relevance-ranked context for one task; never dumps all memory."""
    cfg = load_config(db.paths)
    budget = budget or cfg["context"]["brief_budget_chars"]
    t = tasks.get(db, task_id)
    terms = set(re.findall(r"[a-z0-9_]{3,}", f"{t['title']} {t['description']} {t['files']}".lower()))
    parts = [f"# Task {t['id']}: {t['title']} [{t['status']}]\n{t['description']}\nAcceptance: {t['acceptance'] or 'n/a'}\nOwned files: {t['files']}"]
    deps = db.q("SELECT t.id,t.title,t.status,t.notes FROM task_deps d JOIN tasks t ON t.id=d.dep_id WHERE d.task_id=?", (task_id,))
    if deps:
        parts.append("## Dependencies\n" + "\n".join(f"- {d['id']} [{d['status']}] {d['title']}" for d in deps))
    # relevant facts and ADRs, ranked
    scored = []
    proj, _ = memory.read(db, "project")
    for ln in proj.splitlines():
        if ln.startswith("- ["):
            scored.append((_score(ln, terms), ln))
    for f in sorted((db.paths.memory / "decisions").glob("ADR-*.md")) if (db.paths.memory / "decisions").exists() else []:
        txt = f.read_text()
        scored.append((_score(txt, terms) + 1, f"ADR {f.stem}: " + txt.split("\n## Decision\n")[-1][:300].replace("\n", " ")))
    top = [s for sc, s in sorted(scored, key=lambda x: -x[0]) if sc > 0][:12]
    if top:
        parts.append("## Relevant project memory (verify before relying)\n" + "\n".join(top))
    others = db.q("SELECT id,title,session_id,files FROM tasks WHERE status IN ('ASSIGNED','IN_PROGRESS') AND id!=?", (task_id,))
    if others:
        parts.append("## Other agents are working on\n" + "\n".join(f"- {o['id']} {o['title']} (by {o['session_id']}) files={o['files']}" for o in others))
    msgs = bus.inbox(db, who)[:8]
    if msgs:
        parts.append(bus.render_untrusted(msgs))
    text = "\n\n".join(parts)
    return text if len(text) <= budget else text[:budget] + "\n…[truncated to budget]"


def evaluate(db: DB, session: str, task_id: str | None, pct: float | None, source: str) -> dict:
    """Decide threshold action. Auto-checkpoints at checkpoint_pct. Returns {'level','message','checkpoint'}."""
    cfg = load_config(db.paths)["context"]
    res = {"level": "ok", "message": "", "checkpoint": None, "pct": pct, "source": source}
    if pct is None:
        res["level"] = "unknown"
        return res
    tag = "" if source == "reported" else " (estimated)"
    if pct >= cfg["checkpoint_pct"]:
        recent = db.one("SELECT created FROM checkpoints WHERE session_id=? ORDER BY created DESC LIMIT 1", (session,))
        from .db import now
        if not recent or now() - recent["created"] > 300:
            res["checkpoint"] = checkpoint.create(db, session, task_id, {"current_stage": f"auto-checkpoint at context {pct:.0f}%{tag}"}, reason="context-threshold")
        res["level"] = "critical"
        res["message"] = (f"Context at {pct:.0f}%{tag}. State checkpointed ({res['checkpoint'] or 'recent'}). Finish the current step, "
                          f"record decisions with `mesh checkpoint create --next ...`, then continue in a fresh session via `mesh resume {task_id or ''}`.")
    elif pct >= cfg["warn_pct"]:
        res["level"] = "warn"
        res["message"] = f"Context at {pct:.0f}%{tag}. Save progress with `mesh checkpoint create` soon."
    return res


def rollover_prompt(db: DB, task_id: str | None, who: str = "new-session") -> str:
    return (checkpoint.brief(db, task_id, who) +
            "\n\n## Instructions for the new session\n1. Register: `mesh session start`. 2. Claim the task if it is READY: `mesh task claim <id>`."
            "\n3. Verify repo drift above, then continue from 'next_actions' without repeating completed_steps.")
