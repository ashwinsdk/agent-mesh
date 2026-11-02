"""Slash-command parsing and execution for the app. Pure Python (no Textual) so it is testable headlessly.

Scheduling changes are never made here: they are enqueued for the conductor (single writer)."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .. import adapters, conductor, runs
from ..db import DB, MeshError, Paths

COMMANDS = [
    ("/new", "<prompt>", "start a new project run (or just type your prompt)"),
    ("/approve", "", "approve the first item that needs you (plan, review, promote)"),
    ("/reject", "[reason]", "reject it (a rejected plan is replanned with your reason)"),
    ("/plan", "", "show the current plan"),
    ("/replan", "[feedback]", "discard the plan and plan again"),
    ("/pause", "", "stop dispatching and approving (running workers continue)"),
    ("/resume", "", "continue after /pause or /halt"),
    ("/halt", "", "stop every worker now"),
    ("/agents", "", "focus the agents list (enter opens one)"),
    ("/agent", "<id|#>", "open an agent: live output, restart, terminate, message"),
    ("/task", "<id|#>", "open a task: diff, verification, approve/reject"),
    ("/retry", "<task>", "re-queue a failed or blocked task"),
    ("/kill", "<worker>", "terminate one worker"),
    ("/say", "<worker> <text>", "send a message to a running worker"),
    ("/cli", "<claude|copilot|antigravity>", "switch the orchestrator's CLI"),
    ("/usage", "", "tokens, cost and limits per CLI / model / agent / task"),
    ("/sessions", "", "session history"),
    ("/promote", "", "merge the integration branch into your main branch"),
    ("/report", "", "plain-text project report"),
    ("/settings", "", "re-run setup (CLI, billing, autonomy)"),
    ("/stop", "", "stop the background conductor (workers keep their current turn)"),
    ("/help", "", "this list"),
    ("/quit", "", "leave the app (the conductor keeps working)"),
]
NAMES = [c[0] for c in COMMANDS]


@dataclass
class Reply:
    kind: str = "message"      # message | screen | quit | focus
    text: str = ""
    level: str = "info"        # info | ok | warn | bad
    screen: str = ""
    arg: str = ""


def parse(text: str) -> tuple[str | None, str]:
    text = text.strip()
    if not text:
        return None, ""
    if not text.startswith("/"):
        return "/new", text
    head, _, rest = text.partition(" ")
    return head.lower(), rest.strip()


def _ensure_conductor(paths: Paths) -> None:
    conductor.start_daemon(paths)


def resolve(snap: dict, token: str, kind: str) -> str | None:
    """Accept a full id, a unique prefix, or a #number as shown in the app."""
    token = token.strip()
    if kind == "worker":
        rows = [a for a in snap["agents"] if a["kind"] in ("worker", "orchestrator", "session")]
        ids = [a["id"] for a in rows]
    else:
        rows = [t for t in snap["tasks"]["list"] if t["kind"] not in ("epic", "project")]
        ids = [t["id"] for t in rows]
    if token.startswith("#") and token[1:].isdigit():
        i = int(token[1:]) - 1
        return ids[i] if 0 <= i < len(ids) else None
    if token in ids:
        return token
    hits = [i for i in ids if i.startswith(token)]
    return hits[0] if len(hits) == 1 else None


def execute(paths: Paths, snap: dict, text: str) -> Reply:
    cmd, arg = parse(text)
    if cmd is None:
        return Reply("message", "")
    db = DB(paths)
    try:
        def q(kind, **payload):
            runs.enqueue(db, kind, payload, "human")
            _ensure_conductor(paths)

        def first_attention(*kinds):
            return next((a for a in snap["attention"] if not kinds or a["kind"] in kinds), None)

        if cmd == "/new":
            if not arg:
                return Reply("message", "Usage: /new <what you want built>", "warn")
            cfg_cli = snap.get("run") and snap["run"]["status"] not in ("complete", "failed", "halted")
            q("submit_prompt", prompt=arg)
            return Reply("message", "Sent to the orchestrator." if cfg_cli else "Starting a new run…", "ok")
        if cmd == "/approve":
            a = first_attention()
            if not a:
                return Reply("message", "Nothing is waiting for you.", "info")
            q("approve_item", id=a["id"])
            return Reply("message", f"Approving: {a['title'][:80]}", "ok")
        if cmd == "/reject":
            a = first_attention("plan_approval", "task_review", "promote") or first_attention()
            if not a:
                return Reply("message", "Nothing is waiting for you.", "info")
            q("reject_item", id=a["id"], note=arg)
            return Reply("message", f"Rejecting: {a['title'][:80]}", "warn")
        if cmd == "/promote":
            a = first_attention("promote")
            if not a:
                return Reply("message", "There is nothing to promote yet (the run must finish first).", "warn")
            q("approve_item", id=a["id"])
            return Reply("message", "Promoting the integration branch…", "ok")
        if cmd in ("/pause", "/resume", "/halt", "/stop"):
            q({"/pause": "pause", "/resume": "resume", "/halt": "halt", "/stop": "stop"}[cmd])
            return Reply("message", {"/pause": "Pausing…", "/resume": "Resuming…", "/halt": "Halting every worker…", "/stop": "Stopping the conductor…"}[cmd],
                         "warn" if cmd in ("/halt", "/pause", "/stop") else "ok")
        if cmd == "/replan":
            q("replan", feedback=arg)
            return Reply("message", "Replanning…", "info")
        if cmd == "/cli":
            if arg not in ("claude", "copilot", "antigravity"):
                return Reply("message", "Usage: /cli claude|copilot|antigravity", "warn")
            if not adapters.ADAPTERS[arg](paths).available():
                return Reply("message", f"{arg} is not installed on this machine.", "bad")
            q("set_cli", cli=arg)
            return Reply("message", f"Orchestrator CLI → {arg}", "ok")
        if cmd == "/retry":
            tid = resolve(snap, arg, "task")
            if not tid:
                return Reply("message", "Unknown task. Use /task to see them.", "warn")
            q("retry_task", task=tid)
            return Reply("message", f"Re-queuing {tid}", "ok")
        if cmd == "/kill":
            wid = resolve(snap, arg, "worker")
            if not wid:
                return Reply("message", "Unknown worker.", "warn")
            q("kill_worker", worker=wid)
            return Reply("message", f"Terminating {wid}", "warn")
        if cmd == "/say":
            tok, _, msg = arg.partition(" ")
            wid = resolve(snap, tok, "worker")
            if not wid or not msg.strip():
                return Reply("message", "Usage: /say <worker> <message>", "warn")
            q("say", worker=wid, text=msg.strip())
            return Reply("message", f"Message queued for {wid}", "ok")
        if cmd == "/agent":
            wid = resolve(snap, arg, "worker") if arg else None
            return Reply("screen", screen="agent", arg=wid) if wid else Reply("message", "Usage: /agent <id|#>", "warn")
        if cmd == "/task":
            tid = resolve(snap, arg, "task") if arg else None
            return Reply("screen", screen="task", arg=tid) if tid else Reply("message", "Usage: /task <id|#>", "warn")
        if cmd == "/agents":
            return Reply("focus", arg="agents")
        if cmd in ("/usage", "/sessions", "/settings", "/help", "/plan", "/report"):
            return Reply("screen", screen=cmd[1:])
        if cmd == "/quit":
            return Reply("quit")
        return Reply("message", f"Unknown command {cmd}. Type /help.", "warn")
    except MeshError as e:
        return Reply("message", str(e), "bad")
    finally:
        db.close()
