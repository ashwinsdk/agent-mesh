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
prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
fmt = ("claude" if ("--verbose" in argv or "--allowedTools" in argv) else
       "antigravity" if ("--dangerously-skip-permissions" in argv or "--mode" in argv) else
       "copilot" if "--allow-all-tools" in argv else "claude")
wid = os.environ.get("MESH_WORKER", "w-none")
sid = f"fake-{fmt}-{wid}"
state_dir = os.environ.get("FAKE_STATE_DIR", "/tmp")
cf = os.path.join(state_dir, f"{wid}.{fmt}.n")
n = int(open(cf).read()) if os.path.exists(cf) else 0
open(cf, "w").write(str(n + 1))
if "--resume" in argv or "--conversation" in argv:
    sid = (argv[argv.index("--resume") + 1] if "--resume" in argv else argv[argv.index("--conversation") + 1])

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if prompt == "/usage":  # the CLI's own usage command: no model, just a report (format is whatever FAKE_USAGE says)
    sys.stdout.write(os.environ.get("FAKE_USAGE", "Usage\n  Daily: 38% used · resets in 5h 10m\n  Weekly: 12% used · resets Mon 09:00\n") + "\n")
    sys.exit(0)


def _nth(kind):
    p_ = os.path.join(state_dir, f"calls.{kind}")
    n_ = int(open(p_).read()) if os.path.exists(p_) else 0
    open(p_, "w").write(str(n_ + 1))
    return n_


if "MESH_PLAN_REQUEST" in prompt or "MESH_BRAIN_TURN" in prompt:
    import re
    kind = "plan" if "MESH_PLAN_REQUEST" in prompt else "brain"
    n_ = _nth(kind)
    seq = json.loads(os.environ.get(f"FAKE_{kind.upper()}_SEQ", "null"))
    one = json.loads(os.environ.get(f"FAKE_{kind.upper()}", "null"))
    body = (seq[min(n_, len(seq) - 1)] if seq else one)
    if body is None:
        body = {"actions": []} if kind == "brain" else {}
    if os.environ.get("FAKE_PLANNER_FAIL"):
        sys.stderr.write("planner exploded\n"); sys.exit(2)
    text = body if isinstance(body, str) else json.dumps(body)
    if isinstance(body, dict) and kind == "brain":
        m = re.search(r"\[(M-[0-9a-f]{8})\]", prompt)
        text = text.replace("$MSG", m.group(1) if m else "M-none")
        body = json.loads(text)
    struct = os.environ.get("FAKE_STRUCT", "1") != "0" and isinstance(body, dict)
    psid = f"fake-{kind}-session"
    if fmt == "claude":
        out({"type": "result", "subtype": "success", "is_error": False, "result": "" if struct else text, "session_id": psid, "total_cost_usd": 0.02,
             "usage": {"input_tokens": 100, "output_tokens": 50}, **({"structured_output": body} if struct else {})})
    elif fmt == "antigravity":
        out({"event": "init", "conversation_id": psid, "init": {}})
        out({"event": "result", "result": {"conversation_id": psid, "status": "SUCCESS", "response": text, **({"structured_output": body} if struct else {}),
                                           "usage": {"input_tokens": 100, "output_tokens": 50}}})
    else:
        out({"type": "assistant.message", "data": {"content": "```json\n" + text + "\n```", "toolRequests": []}})
        out({"type": "session.usage_checkpoint", "data": {"totalNanoAiu": 100000000}})
        out({"type": "result", "sessionId": psid, "exitCode": 0, "usage": {}})
    sys.exit(0)

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
        out({"event": "init", "conversation_id": sid, "init": {"cwd": os.getcwd(), "tools": [], "permission_mode": "always-proceed", "model": "fake-model"}})
    else:
        out({"type": "session.tools_updated", "data": {"model": "fake-model"}})


def emit_text(t, tool=None, inp=None):
    if fmt == "claude":
        content = [{"type": "tool_use", "name": tool, "input": inp or {}}] if tool else [{"type": "text", "text": t}]
        out({"type": "assistant", "message": {"id": f"m{time.time()}", "model": "fake-model", "content": content,
                                               "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}})
        tot["input"] += 10; tot["output"] += 5; tot["cache_read"] += 100
    elif fmt == "antigravity":
        tot["input"] += 10; tot["output"] += 5
        su = {"conversation_id": sid, "step_index": 1, "state": "ACTIVE" if tool else "DONE", "step_type": "tool" if tool else "agent_response",
              "usage": {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 15}}
        if tool:
            su.update({"tool_name": tool, "tool_info": {"name": tool, "parameters": inp or {}}})
        else:
            su["text_delta"] = t
        out({"event": "step_update", "step_update": su})
    else:
        if tool:
            out({"type": "tool.execution_start", "data": {"toolName": tool, "arguments": inp or {}}})
        else:
            out({"type": "assistant.message_delta", "data": {"deltaContent": t}})


def finish(ok=True, err=None, code=0):
    global cost
    if fmt == "claude":
        r = {"type": "result", "subtype": "success" if ok else "error_during_execution", "is_error": not ok, "session_id": sid,
             "total_cost_usd": cost or 0.01, "num_turns": 1, "result": err or "done",
             "usage": {"input_tokens": tot["input"], "output_tokens": tot["output"], "cache_read_input_tokens": tot["cache_read"]},
             "modelUsage": {"fake-model": {"contextWindow": 200000}}}
        out(r)
    elif fmt == "antigravity":
        out({"event": "result", "result": {"conversation_id": sid, "status": "SUCCESS" if ok else "ERROR", "response": "done", "error": err,
                                           "num_turns": 1, "usage": {"input_tokens": tot["input"], "output_tokens": tot["output"]}}})
    else:
        if err:
            out({"type": "session.error", "data": {"message": err}})
        out({"type": "session.usage_checkpoint", "data": {"totalNanoAiu": 500000000, "totalPremiumRequests": 0}})
        out({"type": "result", "sessionId": sid, "exitCode": code, "usage": {"premiumRequests": 0}})
        p = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH")
        if p:
            a = {"gen_ai.conversation.id": sid, "gen_ai.usage.input_tokens": 120, "gen_ai.usage.output_tokens": 30,
                 "gen_ai.usage.cache_read.input_tokens": 10, "github.copilot.nano_aiu": 500000000.0}
            with open(p, "a") as f:
                f.write(json.dumps({"type": "span", "name": "chat auto", "attributes": {**a, "gen_ai.operation.name": "chat"}}) + "\n")
                f.write(json.dumps({"type": "span", "name": "invoke_agent", "attributes": {**a, "gen_ai.operation.name": "invoke_agent"}}) + "\n")
    sys.exit(code)


_ti = {}


def task_info():
    if not _ti:
        mesh = os.environ.get("FAKE_MESH_CMD", "python3 -m mesh")
        r = subprocess.run(f"{mesh} task show \"$MESH_TASK\"", shell=True, capture_output=True, text=True)
        try:
            _ti.update(json.loads(r.stdout))
        except ValueError:
            _ti.update({"title": "", "files": "[]"})
    return _ti


emit_init()
for step in sc:
    if step.get("when_title") and step["when_title"] not in task_info().get("title", ""):
        continue
    if step.get("hook"):  # simulate a CLI that fires the project's PostToolUse hook and shows injected context to the model
        payload = {"session_id": sid, "sessionId": sid, "conversationId": sid, "cwd": os.getcwd(), "tool_name": "Bash",
                   "toolCall": {"name": "run_command", "args": {}}}
        ev = "pre_invocation" if fmt == "antigravity" else "post_tool"
        r = subprocess.run(f".mesh/bin/mesh hook {fmt} {ev}", shell=True, input=json.dumps(payload), capture_output=True, text=True)
        txt = r.stdout
        try:
            j = json.loads(r.stdout)
            txt = j.get("hookSpecificOutput", {}).get("additionalContext") or j.get("additionalContext") or \
                (j.get("injectSteps") or [{}])[0].get("systemMessage", {}).get("systemMessage") or ""
        except ValueError:
            pass
        open(step["hook"], "w").write(txt if "PROBE-TOKEN" in txt else "none")
    if step.get("owned_file"):
        pat = json.loads(task_info().get("files") or "[]")[0]
        d = pat[:-3] if pat.endswith("/**") else os.path.dirname(pat)
        path = os.path.join(d, os.environ.get("MESH_TASK", "t") + ".txt") if d else os.environ.get("MESH_TASK", "t") + ".txt"
        emit_text("", "Write", {"file_path": path})
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        open(path, "w").write(task_info().get("title", "") + "\n")
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
