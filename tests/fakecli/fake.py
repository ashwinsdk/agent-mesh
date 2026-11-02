#!/usr/bin/env python3
"""Fake agent CLI for deterministic tests. Emulates the headless output of claude / agy / copilot.

Format is inferred from the flags mesh passes (same detection real adapters would produce):
  --verbose + stream-json -> claude;  --dangerously-skip-permissions -> antigravity;  --allow-all-tools -> copilot.
Behaviour comes from $FAKE_SCENARIO (JSON), a list of steps, or {"attempts":[steps,...]}, or {"by_cli":{fmt:steps}}.
Steps: say, write{path,content}, sleep, sh (with {mesh} placeholder), commit, submit, usage{input,output,cache_read},
       limit, crash, exit{code}, cost, stall.
"""
import json
import os
import subprocess
import sys
import time

argv = sys.argv[1:]
fmt = "claude" if "--verbose" in argv else "antigravity" if "--dangerously-skip-permissions" in argv else "copilot" if "--allow-all-tools" in argv else "claude"
wid = os.environ.get("MESH_WORKER", "w-none")
sid = f"fake-{fmt}-{wid}"
state_dir = os.environ.get("FAKE_STATE_DIR", "/tmp")
cf = os.path.join(state_dir, f"{wid}.{fmt}.n")
n = int(open(cf).read()) if os.path.exists(cf) else 0
open(cf, "w").write(str(n + 1))
if "--resume" in argv or "--conversation" in argv:
    sid = (argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--conversation") + 1])

sc = json.loads(os.environ.get("FAKE_SCENARIO", "null")) or [{"write": "out.txt", "content": "hello\n"}, {"commit": True}, {"submit": True}]
if isinstance(sc, dict) and "by_cli" in sc:
    sc = sc["by_cli"].get(fmt, [])
if isinstance(sc, dict) and "attempts" in sc:
    sc = sc["attempts"][min(n, len(sc["attempts"]) - 1)]

tot = {"input": 0, "output": 0, "cache_read": 0}
cost = 0.0


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def emit_init():
    if fmt == "claude":
        out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model"})
    elif fmt == "antigravity":
        out({"event": "init", "model": "fake-model", "conversation_id": sid})
    else:
        out({"type": "init", "sessionId": sid, "model": "fake-model"})


def emit_text(t, tool=None, inp=None):
    if fmt == "claude":
        content = [{"type": "tool_use", "name": tool, "input": inp or {}}] if tool else [{"type": "text", "text": t}]
        out({"type": "assistant", "message": {"id": f"m{time.time()}", "model": "fake-model", "content": content,
                                               "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}})
        tot["input"] += 10; tot["output"] += 5; tot["cache_read"] += 100
    elif fmt == "antigravity":
        tot["input"] += 10; tot["output"] += 5
        ev = {"event": "step_update", "conversation_id": sid, "step_type": "tool" if tool else "agent_response", "state": "DONE",
              "usage": {"input_tokens": tot["input"], "output_tokens": tot["output"], "total_tokens": tot["input"] + tot["output"]}}
        if tool:
            ev.update({"tool_name": tool, "tool_info": {"name": tool, "parameters": inp or {}}})
        else:
            ev["text_delta"] = t
        out(ev)
    else:
        out({"type": "tool_call" if tool else "message", "sessionId": sid, "toolName": tool, "text": t})


def finish(ok=True, err=None, code=0):
    global cost
    if fmt == "claude":
        r = {"type": "result", "subtype": "success" if ok else "error_during_execution", "is_error": not ok, "session_id": sid,
             "total_cost_usd": cost or 0.01, "num_turns": 1, "result": err or "done",
             "usage": {"input_tokens": tot["input"], "output_tokens": tot["output"], "cache_read_input_tokens": tot["cache_read"]},
             "modelUsage": {"fake-model": {"contextWindow": 200000}}}
        out(r)
    elif fmt == "antigravity":
        out({"event": "result", "conversation_id": sid, "status": "SUCCESS" if ok else "ERROR", "response": "done", "error": err,
             "num_turns": 1, "usage": {"input_tokens": tot["input"], "output_tokens": tot["output"]}})
    else:
        out({"type": "result", "sessionId": sid, "result": "done", "error": err})
        p = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
        if p:
            with open(p, "a") as f:
                f.write(json.dumps({"attributes": {"gen_ai.usage.input_tokens": 120, "gen_ai.usage.output_tokens": 30}}) + "\n")
    sys.exit(code)


emit_init()
for step in sc:
    if "say" in step:
        emit_text(step["say"])
    if "write" in step:
        emit_text("", "Write", {"file_path": step["write"]})
        os.makedirs(os.path.dirname(step["write"]) or ".", exist_ok=True)
        open(step["write"], "a" if step.get("append") else "w").write(step.get("content", ""))
    if "sleep" in step:
        time.sleep(step["sleep"])
    if "usage" in step:
        tot["input"] += step["usage"].get("input", 0); tot["output"] += step["usage"].get("output", 0)
    if "cost" in step:
        cost = step["cost"]
    if "sh" in step or step.get("commit") or step.get("submit"):
        if "sh" in step:
            cmd = step["sh"]
        elif step.get("commit"):
            cmd = "git add -A && git -c user.email=f@f -c user.name=fake commit -qm 'fake work' || true"
        else:
            cmd = "{mesh} task submit \"$MESH_TASK\" --note 'fake handoff'"
        cmd = cmd.replace("{mesh}", os.environ.get("FAKE_MESH_CMD", "python3 -m mesh"))
        emit_text("", "Bash", {"command": cmd})
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if r.returncode != 0 and not step.get("allow_fail"):
            sys.stderr.write(r.stderr)
            finish(False, f"step failed: {cmd}: {r.stderr[-200:]}", 1)
    if step.get("limit"):
        if fmt == "claude":
            out({"type": "system", "subtype": "api_retry", "error": "rate_limit", "error_status": 429, "attempt": 1, "session_id": sid})
        finish(False, "usage limit reached: rate limit exceeded (429)", 1)
    if step.get("crash"):
        os._exit(137)
    if "exit" in step:
        finish(step["exit"] == 0, None if step["exit"] == 0 else "exit", step["exit"])
    if step.get("stall"):
        time.sleep(3600)
finish()
