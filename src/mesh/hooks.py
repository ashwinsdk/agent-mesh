"""Hook dispatcher: one entry point for all CLIs. Fail-open on internal errors; never rewrites agent output."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

from . import bus, checkpoint, context, eventlog, locks, tasks, usage
from .core import end_session, heartbeat, load_config, register_session
from .db import DB, MeshError, Paths, find_root
from .safety import check_command, check_path_write, scan_secrets

EDIT_KEYS = ("file_path", "notebook_path", "path", "filePath", "TargetFile", "target_file", "AbsolutePath", "Path", "file", "filename")
CONTENT_KEYS = ("content", "new_string", "new_str", "file_text", "CodeContent", "ReplacementContent", "text")
CMD_KEYS = ("command", "CommandLine", "cmd", "script")
SHELL_TOOLS = re.compile(r"^(bash|shell|run_command|powershell|execute_bash|terminal)$", re.I)
EDIT_TOOLS = re.compile(r"(edit|write|create|replace|patch|apply|multiedit|notebook|sed_file)", re.I)


def normalize(cli: str, event: str, p: dict) -> dict:
    """Map each client's payload to one shape. Unknown fields are tolerated (None)."""
    if cli == "antigravity":
        tc = p.get("toolCall") or {}
        args = tc.get("args") or {}
        native, tool, transcript, model = p.get("conversationId"), tc.get("name"), p.get("transcriptPath"), p.get("modelName")
        cwd = (p.get("workspacePaths") or [None])[0]
    else:
        native = p.get("session_id") or p.get("sessionId")
        tool = p.get("tool_name") or p.get("toolName")
        args = p.get("tool_input") or p.get("toolArgs") or p.get("tool_args") or {}
        transcript = p.get("transcript_path") or p.get("transcriptPath")
        model = (p.get("model") if isinstance(p.get("model"), str) else None)
        cwd = p.get("cwd")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {"command": args}
    if not isinstance(args, dict):
        args = {}
    return {"native": native, "tool": tool or "", "args": args, "transcript": transcript, "model": model, "cwd": cwd,
            "reason": p.get("reason") or p.get("source")}


def _first(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _rel(root: Path, path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, OSError):
        return path


def _out(cli: str, kind: str, text: str = "", deny: str | None = None) -> tuple[str, int]:
    """Build the CLI-specific response. kind: context | deny | none."""
    if kind == "deny":
        if cli == "claude":
            return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                      "permissionDecisionReason": deny}}), 0
        if cli == "copilot":
            return json.dumps({"permissionDecision": "deny", "permissionDecisionReason": deny}), 0
        return json.dumps({"decision": "deny", "reason": deny}), 0
    if kind == "post_context" and text:
        if cli == "claude":
            return json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}), 0
        if cli == "copilot":
            return json.dumps({"additionalContext": text}), 0
        return json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]}), 0
    if kind == "context" and text:
        if cli == "claude":
            return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}), 0
        if cli == "copilot":
            return json.dumps({"additionalContext": text}), 0
        return json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]}), 0
    return "", 0


def handle(cli: str, event: str, payload: dict, root: Path | None = None) -> tuple[str, int]:
    root = root or find_root(payload.get("cwd") or (payload.get("workspacePaths") or [None])[0])
    paths = Paths(root)
    db = DB(paths, busy_ms=2000)  # hooks must never stall the CLI
    try:
        n = normalize(cli, event, payload)
        cfg = load_config(paths)
        env_sid = os.environ.get("MESH_SESSION")  # set by `mesh run`: keep one identity per launched session
        if env_sid and db.one("SELECT 1 FROM sessions WHERE id=?", (env_sid,)):
            sid = env_sid
            if event != "session_end":
                heartbeat(db, sid)
        elif event != "session_end":
            sid = register_session(db, cli, os.environ.get("MESH_AGENT") or None, n["native"] or f"pid{os.getppid()}",
                                   os.getppid(), n["model"])
        else:
            sid = f"{cli}-{(n['native'] or '')[:8]}"
        if event == "session_start":
            return _session_start(db, cli, sid, n, cfg)
        if event == "session_end":
            if db.one("SELECT 1 FROM sessions WHERE id=?", (sid,)):
                end_session(db, sid)
            return "", 0
        if event == "pre_tool":
            return _pre_tool(db, cli, sid, n, cfg)
        if event in ("post_tool", "pre_invocation"):
            if event == "post_tool":
                db.event(sid, "tool", None, {"tool": n["tool"]})
            if cli == "antigravity" and event == "post_tool":
                return "", 0  # Antigravity can only inject at PreInvocation; consuming the mailbox here would lose messages
            txt = digest(db, sid)
            if txt:
                return _out(cli, "post_context", txt)
            return "", 0
        if event == "pre_compact":
            row = db.one("SELECT task_id FROM sessions WHERE id=?", (sid,))
            checkpoint.create(db, sid, row["task_id"] if row else None,
                              {"current_stage": "automatic checkpoint before context compaction"}, reason="pre-compact")
            return "", 0
        if event == "stop":
            heartbeat(db, sid)
            locks.heartbeat(db, sid, cfg["locks"]["ttl_seconds"])
            return "", 0
        if event == "error":
            txt = json.dumps(payload)[:500]
            if re.search(r"rate.?limit|429|quota|usage limit|too many requests", txt, re.I):
                usage.record_rate_limit(db, sid, cli, txt)
            return "", 0
        return "", 0
    finally:
        db.close()


def digest(db: DB, sid: str, max_msgs: int = 5) -> str:
    """Mailbox + (for the orchestrator) event digest, injected at the agent's next tool/invocation boundary.
    Messages are injected once (delivered_at); unacked ones stay in `mesh msg inbox` and are retried by bus.sweep."""
    parts = []
    rows = db.q("SELECT m.* FROM messages m WHERE m.delivered_at IS NULL AND m.delivery_state!='failed' AND m.recipient IN (%s) ORDER BY m.priority, m.ts LIMIT ?"
                % ",".join("?" * len(bus._targets(db, sid))), (*bus._targets(db, sid), max_msgs))
    rows = [r for r in rows if sid not in json.loads(r["ack_by"] or "[]") and r["sender"] != sid and r["recipient"] != "*"]
    if rows:
        parts.append(bus.render_untrusted(rows))
        parts.append("[mesh] Reply with `mesh msg reply <id> \"...\"`; acknowledge with `mesh msg ack <id>`.")
        bus.mark_delivered(db, [r["id"] for r in rows])
    s = db.one("SELECT role FROM sessions WHERE id=?", (sid,))
    if s and s["role"] == "orchestrator":
        cur = eventlog.get_cursor(db, f"inject:{sid}")
        top = eventlog.max_seq(db)
        if top > cur:
            ev = eventlog.since(db, cur, limit=10, targets=eventlog._targets(db, "orchestrator", sid), own=sid)
            eventlog.set_cursor(db, f"inject:{sid}", top)
            if ev:
                parts.append(bus.fmt_event_digest(eventlog.render(db, ev)))
                parts.append("[mesh] Handle these: `mesh wait --consumer orchestrator` to continue supervising.")
    return "\n".join(parts)


def _session_start(db: DB, cli: str, sid: str, n: dict, cfg: dict) -> tuple[str, int]:
    row = db.one("SELECT task_id FROM sessions WHERE id=?", (sid,))
    lines = [f"[mesh] You are mesh session `{sid}` (cli={cli}). Pass `--as {sid}` to mesh commands, or `export MESH_SESSION={sid}`.",
             "[mesh] Protocol: see AGENTS.md. Check work with `mesh task ready`, claim with `mesh task claim <id>`, "
             "checkpoint with `mesh checkpoint create`."]
    if row and row["task_id"]:
        lines.append(f"[mesh] Your current task: {row['task_id']}. Resume context: `mesh resume {row['task_id']}`.")
    held = tasks.abandoned(db, cfg["tasks"]["stale_after_seconds"])
    if held:
        lines.append("[mesh] Interrupted tasks available to resume: " + ", ".join(f"{t['id']} ({t['title'][:40]})" for t in held[:5])
                     + ". Use `mesh task resume <id>`.")
    msgs = bus.inbox(db, sid)
    if msgs:
        lines.append(f"[mesh] {len(msgs)} unread message(s). Read: `mesh msg inbox`.")
        lines.append(bus.render_untrusted(msgs[:3]))
    return _out(cli, "context", "\n".join(lines))


def _pre_tool(db: DB, cli: str, sid: str, n: dict, cfg: dict) -> tuple[str, int]:
    args, tool, root = n["args"], n["tool"], db.paths.root
    pol = cfg["policy"]
    cmd = _first(args, CMD_KEYS) if (SHELL_TOOLS.search(tool) or not EDIT_TOOLS.search(tool)) else None
    if cmd and pol["destructive"] != "allow":
        why = check_command(cmd)
        if why:
            db.event(sid, "safety.deny", None, {"why": why, "cmd": cmd[:120]})
            return _out(cli, "deny", deny=f"mesh safety: {why} requires explicit human approval. Ask the user, or have them run it.")
    path = _first(args, EDIT_KEYS)
    if path and EDIT_TOOLS.search(tool):
        rel = _rel(root, path)
        if pol["protect_mesh_files"]:
            why = check_path_write(rel)
            if why:
                db.event(sid, "safety.deny", None, {"why": why, "path": rel})
                return _out(cli, "deny", deny=f"mesh safety: {why}.")
        blk = locks.check_path(db, sid, rel)
        if blk:
            db.event(sid, "lock.denied", blk["task_id"], {"path": rel, "owner": blk["owner"]})
            return _out(cli, "deny", deny=(f"mesh: `{rel}` is locked by {blk['owner']} for task {blk['task_id']}. "
                                           f"Do not edit it. Send `mesh msg send --to {blk['owner']} --type file_request ...` or pick other work."))
        body = _first(args, CONTENT_KEYS)
        if body and pol["secret_in_write"] != "allow" and scan_secrets(body):
            db.event(sid, "safety.deny", None, {"why": "secret in write", "path": rel})
            return _out(cli, "deny", deny="mesh safety: content looks like it contains a secret/credential. Use environment variables instead.")
    return "", 0


def run(cli: str, event: str) -> int:
    """stdin JSON -> stdout JSON. Any internal failure => exit 0, no output (fail open), logged."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        out, code = handle(cli, event, payload)
        if out:
            sys.stdout.write(out)
        return code
    except Exception:
        try:
            root = find_root()
            log = Paths(root).logs
            log.mkdir(parents=True, exist_ok=True)
            with open(log / "hook-errors.log", "a") as f:
                f.write(f"{time.strftime('%F %T')} {cli}/{event}\n{traceback.format_exc()}\n")
        except Exception:
            pass
        return 0


def statusline() -> int:
    """Claude Code statusLine command: records REPORTED usage, evaluates context thresholds, prints one line."""
    try:
        p = json.loads(sys.stdin.read() or "{}")
        root = find_root(p.get("cwd") or (p.get("workspace") or {}).get("current_dir"))
        paths = Paths(root)
        db = DB(paths, busy_ms=1500)
        native = p.get("session_id") or ""
        sid = f"claude-{native[:8]}"
        m = usage.from_claude_statusline(p)
        last = usage.latest(db, sid)
        model = (p.get("model") or {}).get("display_name") if isinstance(p.get("model"), dict) else None
        action = None
        if not last or time.time() - last["ts"] > 5:
            if db.one("SELECT 1 FROM sessions WHERE id=?", (sid,)):
                srow = db.one("SELECT task_id, agent FROM sessions WHERE id=?", (sid,))
                usage.record(db, sid, "claude", "reported", task_id=srow["task_id"], agent=srow["agent"], model=model, **m)
                action = context.evaluate(db, sid, srow["task_id"], m.get("context_pct"), "reported")
        pct = m.get("context_pct")
        bits = [f"mesh:{sid[7:]}"]
        if pct is not None:
            bits.append(f"ctx {pct:.0f}%")
        fh = m.get("five_hour_pct")
        if fh is not None:
            bits.append(f"5h {fh:.0f}%")
        if action and action["message"]:
            bits.append("⚠ checkpointed" if action["checkpoint"] else "⚠ " + action["level"])
        sys.stdout.write(" | ".join(bits))
        db.close()
    except Exception:
        sys.stdout.write("mesh")
    return 0
