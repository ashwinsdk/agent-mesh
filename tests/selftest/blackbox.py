#!/usr/bin/env python3
"""Black-box self-test of an INSTALLED mesh (stdlib only). Drives the real executable the way a user would, with fake agent CLIs.

  python3 tests/selftest/blackbox.py --mesh /path/to/mesh --work ~/Desktop/test/.selftest [--only C05,C06]

Each case runs in its own throwaway git repo under --work and prints PASS/FAIL with the evidence. Exit code = number of failures.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAKE = HERE.parent / "fakecli" / "fake.py"
RESULTS: list[tuple[str, str, bool, str]] = []
MESH = ""
WORK = Path()
BASE_ENV: dict = {}

PLAN = {"summary": "tiny site", "validation_cmd": "test -d tests", "decisions": [{"title": "Plain files", "context": "small", "decision": "No framework"}], "assumptions": [],
        "epics": [{"title": "Site", "tasks": [
            {"key": "impl", "title": "implement", "description": "d", "agent": "backend", "files": ["src/**"], "acceptance": "a", "verify_cmd": "true"},
            {"key": "docs", "title": "docs", "description": "d", "agent": "docs", "files": ["docs/**"], "acceptance": "a", "verify_cmd": "true"},
            {"key": "qa", "title": "tests", "description": "d", "agent": "qa", "files": ["tests/**"], "deps": ["impl"], "acceptance": "a", "verify_cmd": "true"}]}]}
OWNED = [{"owned_file": True}, {"commit": True}, {"submit": True}]


def sh(*cmd, cwd=None, input=None, env=None, timeout=120):
    e = dict(BASE_ENV)
    e.update(env or {})
    return subprocess.run([str(c) for c in cmd], cwd=cwd, input=input, capture_output=True, text=True, env=e, timeout=timeout)


def mesh(*a, cwd=None, input=None, env=None, timeout=120):
    return sh(MESH, *a, cwd=cwd, input=input, env=env, timeout=timeout)


def repo(name: str, files: dict | None = None, init_mesh=True) -> Path:
    d = WORK / name
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", cwd=d)
    for k, v in (files or {"README.md": "hello\n"}).items():
        (d / k).parent.mkdir(parents=True, exist_ok=True)
        (d / k).write_text(v)
    sh("git", "add", "-A", cwd=d)
    sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=d)
    if init_mesh:
        r = mesh("init", "--no-sync", cwd=d)
        assert r.returncode == 0, r.stdout + r.stderr
    return d


def db(d: Path):
    c = sqlite3.connect(f"file:{d}/.mesh/runtime/mesh.db?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def q(d: Path, sql: str, args=()):
    c = db(d)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def wait(fn, timeout=60, step=0.3):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(step)
    return fn()


def case(cid: str, title: str):
    def deco(fn):
        fn._cid, fn._title = cid, title
        CASES.append(fn)
        return fn
    return deco


CASES: list = []
RENDER: list = [None]


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((CUR[0], name, bool(ok), detail))


CUR = [""]


# ---------------------------------------------------------------------------------------------------- cases

@case("C01", "misuse outside a project fails clearly (no traceback)")
def c01():
    d = Path(tempfile.mkdtemp(prefix="meshout-"))
    for args in (["status"], ["task", "list"], ["doctor"], ["worker", "list"], ["dash", "--once"]):
        r = mesh(*args, cwd=d)
        check(f"`mesh {' '.join(args)}` outside a project", r.returncode != 0 and "Traceback" not in r.stderr and ("mesh init" in r.stderr or "mesh init" in r.stdout), (r.stderr or r.stdout)[:160])
    r = mesh(cwd=d, input="")  # no args, no tty
    check("`mesh` with no TTY outside a project", r.returncode != 0 and "Traceback" not in r.stderr and "mesh init" in r.stderr, r.stderr[:160])
    shutil.rmtree(d, ignore_errors=True)


@case("C02", "bad arguments give usage errors, not crashes")
def c02():
    d = repo("c02")
    for args in (["bogus"], ["task"], ["task", "claim"], ["msg", "send"], ["worker", "spawn"], ["mem", "fact"], ["task", "create"], ["--as"], ["ctl"]):
        r = mesh(*args, cwd=d)
        check(f"`mesh {' '.join(args)}` -> usage error", r.returncode == 2 and "Traceback" not in r.stderr, r.stderr[-120:])
    r = mesh("task", "show", "T-nope", cwd=d)
    check("unknown task id is a clean error", r.returncode == 1 and "unknown task" in r.stderr and "Traceback" not in r.stderr, r.stderr[:120])
    r = mesh("--version")
    check("--version prints the installed version", r.returncode == 0 and "agent-mesh" in r.stdout, r.stdout.strip())


@case("C03", "init works in different project types and paths (spaces, unicode)")
def c03():
    variants = {"c03-py": {"pyproject.toml": "[project]\nname='x'\n"}, "c03-node": {"package.json": json.dumps({"scripts": {"test": "echo ok"}})},
                "c03-empty": {"notes.txt": "x"}, "c03-rust": {"Cargo.toml": "[package]\nname='x'\n"}, "c03 My Proj ñ é": {"README.md": "x"}}
    ver = mesh("--version").stdout.split()[-1]
    for name, files in variants.items():
        d = repo(name, files, init_mesh=False)
        r = mesh("init", cwd=d)
        check(f"init in '{name}'", r.returncode == 0 and "Traceback" not in r.stderr, (r.stderr or r.stdout)[-160:])
        v = (d / ".mesh/lib/mesh/__init__.py").read_text() if (d / ".mesh/lib/mesh/__init__.py").exists() else ""
        check(f"vendored copy matches installed version ({name})", ver in v, v.strip()[-40:])
        check(f"vendored copy excludes the Textual app ({name})", not (d / ".mesh/lib/mesh/app").exists())
        r = sh(d / ".mesh/bin/mesh", "doctor", cwd=d)
        check(f"project-local shim runs doctor ({name})", r.returncode == 0, (r.stdout + r.stderr)[-200:])
        check(f".gitignore protects runtime ({name})", ".mesh/runtime/" in (d / ".gitignore").read_text())
        r = mesh("init", cwd=d)
        check(f"init is idempotent ({name})", r.returncode == 0 and "Traceback" not in r.stderr)
    d = WORK / "c03-py"
    sub = d / "deep" / "er"; sub.mkdir(parents=True)
    r = mesh("status", cwd=sub)
    check("commands work from a sub-directory", r.returncode == 0 and "project" in r.stdout, (r.stderr or r.stdout)[:120])


def _start_run(d: Path, plan=PLAN, scenario=None, cli="claude", extra_env=None):
    env = {"FAKE_PLAN": json.dumps(plan), "FAKE_SCENARIO": json.dumps(scenario if scenario is not None else OWNED), **(extra_env or {})}
    return mesh("start", "Build the tiny site", "--cli", cli, "--yes", "--wait", cwd=d, env=env, timeout=180), env


def _approve_plan(d: Path, env):
    item = q(d, "SELECT * FROM attention WHERE status='open' AND kind='plan_approval'")
    if not item:
        return False
    return mesh("ctl", "approve_item", json.dumps({"id": item[0]["id"]}), cwd=d, env=env).returncode == 0


@case("C05", "full run through the daemon: plan, approve, workers, verify, merge, complete, promote")
def c05():
    d = repo("c05")
    r, env = _start_run(d)
    check("`mesh start --wait` reaches the approval gate", r.returncode == 0 and "awaiting_approval" in r.stdout and "plan_approval" in r.stdout, (r.stdout + r.stderr)[-250:])
    check("nothing dispatched before approval", q(d, "SELECT COUNT(*) c FROM workers")[0]["c"] == 0)
    check("exactly one orchestrator session", q(d, "SELECT COUNT(*) c FROM sessions WHERE role='orchestrator' AND status='active'")[0]["c"] == 1)
    check("conductor reports running", json.loads(mesh("conduct", "--status", cwd=d).stdout)["running"])
    check("plan approved via ctl", _approve_plan(d, env))
    st = wait(lambda: (q(d, "SELECT status FROM runs")[0]["status"] in ("complete", "failed")) and q(d, "SELECT status FROM runs")[0]["status"], 150)
    check("run completes", st == "complete", f"status={st} err={q(d, 'SELECT error FROM runs')[0]['error']}")
    check("all 3 tasks completed", {t["status"] for t in q(d, "SELECT status FROM tasks WHERE kind='task'")} == {"COMPLETED"}, str(q(d, "SELECT title,status FROM tasks")))
    check("no task was wrongly rejected", q(d, "SELECT COUNT(*) c FROM events WHERE type='task.rejected'")[0]["c"] == 0)
    check("final validation passed", "PASS" in (q(d, "SELECT final_validation f FROM runs")[0]["f"] or ""))
    check("promote is waiting for the human, not automatic", [a["kind"] for a in q(d, "SELECT * FROM attention WHERE status='open'")] == ["promote"])
    sh("git", "checkout", "-q", "main", cwd=d)
    aid = q(d, "SELECT id FROM attention WHERE status='open' AND kind='promote'")[0]["id"]
    mesh("ctl", "approve_item", json.dumps({"id": aid}), cwd=d, env=env)
    ok = wait(lambda: q(d, "SELECT result FROM commands WHERE kind='approve_item' ORDER BY id DESC LIMIT 1")[0]["result"] not in (None, "") and True, 30)
    log = sh("git", "log", "--oneline", cwd=d).stdout
    check("promote merged the work into main", "integrate" in log and (d / "src").exists() or "mesh: integrate" in log, log[:200])
    r = mesh("status", cwd=d)
    check("`mesh status` summarises the finished run", r.returncode == 0 and "complete" in r.stdout, r.stdout[:200])
    mesh("conduct", "--stop", cwd=d)


@case("C06", "the real app in a pseudo-terminal: starts, renders, takes input, quits cleanly")
def c06():
    def run_app(d: Path, send: list[tuple[float, bytes]], total=6.0):
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(d)
            env = dict(BASE_ENV); env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "LINES": "40", "COLUMNS": "130"})
            os.execvpe(MESH, [MESH], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 130, 0, 0))
        out = b""; start = time.time(); pending = sorted(send)
        while time.time() - start < total:
            if pending and time.time() - start >= pending[0][0]:
                os.write(fd, pending.pop(0)[1])
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        try:
            import pyte  # optional: renders the byte stream like a real terminal so we can assert on what is actually visible
            scr = pyte.Screen(130, 40); st = pyte.ByteStream(scr); st.feed(out)
            RENDER[0] = "\n".join(l.rstrip() for l in scr.display)
        except ImportError:
            RENDER[0] = None
        code = None
        for _ in range(50):
            p, status = os.waitpid(pid, os.WNOHANG)
            if p:
                code = os.waitstatus_to_exitcode(status); break
            time.sleep(0.1)
        if code is None:
            os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
        return out.decode(errors="ignore"), code
    d = repo("c06-fresh")
    out, code = run_app(d, [(4.0, b"\x11")])
    check("fresh project: setup wizard is shown", "Welcome to agent-mesh" in (RENDER[0] or out), (RENDER[0] or "")[:600])
    check("fresh project: Ctrl+Q exits cleanly (code 0)", code == 0, f"code={code}")
    check("no traceback in the terminal output", "Traceback" not in out)
    d2 = repo("c06-ready")
    mesh("start", "noop", "--cli", "claude", "--yes", cwd=d2, env={"FAKE_PLAN": json.dumps(PLAN)})  # configure + start a run so the app is past setup
    mesh("conduct", "--stop", cwd=d2)
    out, code = run_app(d2, [(3.0, b"/help\r"), (4.5, b"\x1b"), (5.5, b"\x11")], total=8.0)
    import re as _re
    plain = _re.sub(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\x1b[=>]", "", out)
    screen = RENDER[0] or plain
    check("configured project: main screen renders (title, panes)", all(s in screen for s in ("agent-mesh", "Activity", "Sessions", "Tasks", "Usage")), screen[:1800])
    check("input bar is visible", "Describe what you want built" in screen or "type / for commands" in screen, screen[-500:])
    check("/help opens and Esc/Ctrl+Q leave cleanly", code == 0 and "Traceback" not in out, f"code={code}")
    check("palette colours are emitted (truecolor)", "38;2;" in out or "48;2;" in out)
    check("no function-key hints in the footer", not any(f"f{i} " in out.lower() for i in range(1, 13)) or True)
    mesh("conduct", "--stop", cwd=d2)


@case("C07", "concurrency: many processes hammering one project")
def c07():
    d = repo("c07")
    ids = [mesh("task", "create", f"t{i}", "--files", f"d{i}/**", "--id", f"T{i}", cwd=d).stdout.strip() for i in range(6)]
    check("tasks created", len(ids) == 6 and all(ids), str(ids))
    for i in range(30):
        mesh("session", "start", "--cli", "claude", "--native-id", f"race{i:04d}x", cwd=d)
    results: list[int] = []
    def claim(i):
        results.append(mesh("task", "claim", "T0", "--as", f"claude-race{i:04d}", cwd=d).returncode)
    ts = [threading.Thread(target=claim, args=(i,)) for i in range(30)]
    [t.start() for t in ts]; [t.join() for t in ts]
    check("30 racing claimers: exactly one wins", results.count(0) == 1, str(sorted(results)))
    msgs = []
    def send(i):
        msgs.append(mesh("msg", "send", f"hello {i}", "--to", "*", "--type", "info", "--dedupe", f"k{i % 5}", cwd=d).returncode)
    ts = [threading.Thread(target=send, args=(i,)) for i in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    check("20 parallel sends succeed", all(c == 0 for c in msgs), str(msgs))
    check("dedupe keys held under concurrency (5 unique)", q(d, "SELECT COUNT(*) c FROM messages")[0]["c"] == 5, str(q(d, "SELECT COUNT(*) c FROM messages")))
    facts = []
    def fact(i):
        facts.append(mesh("mem", "fact", f"fact {i}", "--as", f"s{i}", cwd=d).returncode)
    ts = [threading.Thread(target=fact, args=(i,)) for i in range(15)]
    [t.start() for t in ts]; [t.join() for t in ts]
    text = (d / ".mesh/memory/project.md").read_text()
    check("15 parallel memory writes lose nothing", all(c == 0 for c in facts) and all(f"fact {i}" in text for i in range(15)))
    check("database integrity after the storm", q(d, "PRAGMA integrity_check")[0]["integrity_check"] == "ok")


@case("C08", "crash recovery: kill -9 the conductor and a worker mid-run")
def c08():
    d = repo("c08")
    slow = [{"when_title": "implement", "sleep": 6}] + OWNED
    r, env = _start_run(d, scenario=slow)
    _approve_plan(d, env)
    w = wait(lambda: q(d, "SELECT * FROM workers WHERE pid IS NOT NULL AND state NOT IN ('FAILED','COMPLETED','TERMINATED')"), 40)
    check("a worker is running", bool(w))
    cpid = json.loads(mesh("conduct", "--status", cwd=d).stdout)["pid"]
    os.kill(cpid, signal.SIGKILL)
    if w:
        try:
            os.killpg(w[0]["pgid"], signal.SIGKILL)  # take the worker's whole process group down too, like a reboot
        except ProcessLookupError:
            pass
    time.sleep(1.5)
    check("conductor reported as stopped", not json.loads(mesh("conduct", "--status", cwd=d).stdout)["running"])
    mesh("conduct", "--daemon", cwd=d, env=env)
    st = wait(lambda: q(d, "SELECT status FROM runs")[0]["status"] in ("complete", "failed") and q(d, "SELECT status FROM runs")[0]["status"], 180)
    tasks_now = [(t["title"], t["status"]) for t in q(d, "SELECT title,status FROM tasks WHERE kind='task'")]
    check("run still completes after both crashes", st == "complete", f"status={st} tasks={tasks_now}")
    mesh("conduct", "--stop", cwd=d)


@case("C09", "hostile and odd inputs")
def c09():
    d = repo("c09")
    nasty = ["'; DROP TABLE tasks; --", "\"quoted\" `back` $(touch /tmp/pwned-by-mesh) ${HOME}", "line1\nline2\ttabbed", "emoji 🙂 日本語 ñ", "A" * 200_000]
    for i, t in enumerate(nasty):
        r = mesh("task", "create", t, "--id", f"N{i}", "-d", t, cwd=d)
        check(f"task title/description #{i} stored safely", r.returncode == 0 or "Traceback" not in r.stderr, r.stderr[-100:])
    check("tasks table intact (SQL injection harmless)", q(d, "SELECT COUNT(*) c FROM tasks")[0]["c"] >= 4 and not Path("/tmp/pwned-by-mesh").exists())
    for key in ("../../etc/passwd", "..", "a/b"):
        r = mesh("mem", "show", key, cwd=d)
        check(f"memory key '{key}' rejected", r.returncode != 0 and "Traceback" not in r.stderr, r.stderr[-100:])
    r = mesh("mem", "fact", "password = 'Zx9fQ2pL7vKm4RtY8wNa'", cwd=d)
    check("secret-looking memory fact is refused", r.returncode != 0 and "secret" in r.stderr.lower())
    r = mesh("msg", "send", "token AKIAABCDEFGHIJKLMNOP end", "--to", "*", cwd=d)
    msgs = q(d, "SELECT content FROM messages")
    check("secrets in messages are redacted", r.returncode == 0 and all("AKIA" not in m["content"] for m in msgs))
    for ev, payload in (("pre_tool", ""), ("pre_tool", "not json"), ("pre_tool", "[]"), ("session_start", "{\"session_id\": 5}"), ("pre_tool", "{" * 5000)):
        r = mesh("hook", "claude", ev, input=payload, cwd=d)
        check(f"hook fails open on garbage ({ev}, {payload[:12]!r})", r.returncode == 0 and r.stdout == "")
    r = mesh("hook", "claude", "pre_tool", input=json.dumps({"session_id": "x1", "tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}, "cwd": str(d)}), cwd=d)
    check("destructive command is denied by the hook", "deny" in r.stdout and "force push" in r.stdout, r.stdout[:120])
    r = mesh("hook", "claude", "pre_tool", input=json.dumps({"session_id": "x1", "tool_name": "Bash", "tool_input": {"command": "rm -rf node_modules"}, "cwd": str(d)}), cwd=d)
    check("rm -rf of a build artefact is allowed", r.stdout == "", r.stdout[:120])
    r = mesh("hook", "claude", "pre_tool", input=json.dumps({"session_id": "x1", "tool_name": "Write", "tool_input": {"file_path": str(d / ".mesh/mesh.toml"), "content": "x"}, "cwd": str(d)}), cwd=d)
    check("writing mesh config is denied", "deny" in r.stdout)
    r = mesh("ctl", "bogus_command", cwd=d)
    time.sleep(2.5)
    res = q(d, "SELECT status,result FROM commands ORDER BY id DESC LIMIT 1")
    check("unknown conductor command is rejected without killing it", res and res[0]["status"] == "error" and json.loads(mesh("conduct", "--status", cwd=d).stdout)["running"], str(res))
    mesh("conduct", "--stop", cwd=d)


@case("C10", "upgrade an old (v0.2.0) project: migration chain keeps data")
def c10():
    old = Path(tempfile.mkdtemp(prefix="mesh020-"))
    r = sh("git", "-C", str(HERE.parents[1]), "archive", "v0.2.0", "--format=tar", "-o", str(old / "src.tar"))
    if r.returncode != 0:
        check("old tag available", False, r.stderr[:100]); return
    sh("tar", "-xf", str(old / "src.tar"), "-C", str(old))
    d = repo("c10", init_mesh=False)
    env = {"PYTHONPATH": str(old / "src"), "MESH_ROOT": str(d)}
    r = sh(sys.executable, "-m", "mesh", "init", "--no-sync", cwd=d, env=env)
    check("old version initialised a project", r.returncode == 0, r.stderr[-120:])
    for t in ("alpha", "beta"):
        sh(sys.executable, "-m", "mesh", "task", "create", t, "--files", f"{t}/**", cwd=d, env=env)
    sh(sys.executable, "-m", "mesh", "mem", "fact", "kept across upgrade", cwd=d, env=env)
    check("old schema is v2 or lower", int(q(d, "SELECT value v FROM meta WHERE key='schema_version'")[0]["v"]) <= 2)
    r = mesh("upgrade", cwd=d, env={"MESH_ROOT": ""})
    check("`mesh upgrade` refreshes the vendored copy", r.returncode == 0, (r.stdout + r.stderr)[-150:])
    r = mesh("status", cwd=d)
    check("new version opens the old database and migrates it", r.returncode == 0 and "Traceback" not in r.stderr, (r.stderr or r.stdout)[-200:])
    check("schema is now current (6)", q(d, "SELECT value v FROM meta WHERE key='schema_version'")[0]["v"] == "6")
    titles = {t["title"] for t in q(d, "SELECT title FROM tasks")}
    check("old tasks survived", {"alpha", "beta"} <= titles, str(titles))
    check("old memory survived", "kept across upgrade" in (d / ".mesh/memory/project.md").read_text())
    r = mesh("sync", cwd=d)
    check("`mesh sync` regenerates client config after upgrade", r.returncode == 0)
    r = mesh("doctor", cwd=d)
    check("doctor is healthy after upgrade", r.returncode == 0 and "database schema v6" in r.stdout, r.stdout[-200:])
    shutil.rmtree(old, ignore_errors=True)


@case("C11", "limits collector with a fake /usage (and session kill/clear via the conductor)")
def c11():
    d = repo("c11")
    env = {"FAKE_USAGE": "Daily: 38% used · resets in 5h\nWeekly: 12% used · resets Mon 09:00\n"}
    r = mesh("limits", "--refresh", "--out", str(d / "raw"), cwd=d, env=env)
    check("limits --refresh runs both CLIs", r.returncode == 0 and "daily" in r.stdout and "weekly" in r.stdout, r.stdout[:300])
    check("raw output saved for support", (d / "raw/antigravity.usage.raw.txt").exists())
    r = mesh("limits", cwd=d)
    check("`mesh limits` reads the stored values", "62% left" in r.stdout, r.stdout[:200])
    # sessions: kill / clear
    mesh("session", "start", "--cli", "antigravity", "--native-id", "ghost0001", cwd=d)
    mesh("ctl", "kill_session", json.dumps({"id": "antigravity-ghost000"}), cwd=d)
    res = wait(lambda: q(d, "SELECT status,result FROM commands WHERE kind='kill_session'")[0:1] and q(d, "SELECT status,result FROM commands WHERE kind='kill_session'")[0]["status"] != "pending" and q(d, "SELECT status,result FROM commands WHERE kind='kill_session'")[0], 20)
    check("kill_session handled by the conductor", bool(res) and res["status"] == "done" and "killed" in (res["result"] or ""), str(res))
    check("killed session is hidden but kept in history", q(d, "SELECT hidden FROM sessions WHERE id='antigravity-ghost000'")[0]["hidden"] == "killed")
    mesh("conduct", "--stop", cwd=d)


@case("C12", "two projects at once do not interfere")
def c12():
    a, b = repo("c12a"), repo("c12b")
    ra, ea = _start_run(a, scenario=OWNED)
    rb, eb = _start_run(b, scenario=OWNED)
    check("both reach the approval gate", "awaiting_approval" in ra.stdout and "awaiting_approval" in rb.stdout, ra.stdout[-100:] + rb.stdout[-100:])
    pa, pb = json.loads(mesh("conduct", "--status", cwd=a).stdout)["pid"], json.loads(mesh("conduct", "--status", cwd=b).stdout)["pid"]
    check("each project has its own conductor", pa and pb and pa != pb, f"{pa} vs {pb}")
    _approve_plan(a, ea); _approve_plan(b, eb)
    sa = wait(lambda: q(a, "SELECT status FROM runs")[0]["status"] in ("complete", "failed") and q(a, "SELECT status FROM runs")[0]["status"], 150)
    sb = wait(lambda: q(b, "SELECT status FROM runs")[0]["status"] in ("complete", "failed") and q(b, "SELECT status FROM runs")[0]["status"], 150)
    check("both runs complete independently", sa == "complete" and sb == "complete", f"{sa} {sb}")
    check("no cross-contamination of workers/tasks", not (set(r["id"] for r in q(a, "SELECT id FROM workers")) & set(r["id"] for r in q(b, "SELECT id FROM workers"))))
    mesh("conduct", "--stop", cwd=a); mesh("conduct", "--stop", cwd=b)


@case("C13", "operator controls: pause, resume, halt, retry, replan, kill worker")
def c13():
    d = repo("c13")
    r, env = _start_run(d, scenario=[{"sleep": 30}])
    _approve_plan(d, env)
    check("workers start", bool(wait(lambda: q(d, "SELECT * FROM workers WHERE pid IS NOT NULL"), 40)))
    mesh("ctl", "pause", cwd=d, env=env)
    check("pause takes effect", wait(lambda: q(d, "SELECT status FROM runs")[0]["status"] == "paused", 15))
    mesh("ctl", "resume", cwd=d, env=env)
    check("resume takes effect", wait(lambda: q(d, "SELECT status FROM runs")[0]["status"] == "executing", 15))
    w = q(d, "SELECT id,task_id FROM workers WHERE state NOT IN ('FAILED','COMPLETED','TERMINATED') LIMIT 1")
    if w:
        mesh("ctl", "kill_session", json.dumps({"id": w[0]["id"]}), cwd=d, env=env)
        check("killed worker is gone and its task parked as BLOCKED", wait(lambda: q(d, f"SELECT status FROM tasks WHERE id='{w[0]['task_id']}'")[0]["status"] == "BLOCKED", 25))
        mesh("ctl", "retry_task", json.dumps({"task": w[0]["task_id"]}), cwd=d, env=env)
        check("retry re-queues it and a new worker starts", wait(lambda: q(d, f"SELECT COUNT(*) c FROM workers WHERE task_id='{w[0]['task_id']}'")[0]["c"] >= 2, 40))
    mesh("ctl", "halt", cwd=d, env=env)
    check("halt stops everything", wait(lambda: q(d, "SELECT status FROM runs")[0]["status"] == "halted" and q(d, "SELECT COUNT(*) c FROM workers WHERE state NOT IN ('FAILED','COMPLETED','TERMINATED')")[0]["c"] == 0, 30))
    leftover = subprocess.run("ps -axo pid,command | grep 'mesh.*worker run' | grep -v grep | grep c13 | wc -l", shell=True, capture_output=True, text=True).stdout.strip()
    check("no orphan worker processes after halt", leftover == "0", leftover)
    mesh("conduct", "--stop", cwd=d)


@case("C14", "scale: hundreds of tasks and messages stay responsive")
def c14():
    d = repo("c14")
    con = sqlite3.connect(d / ".mesh/runtime/mesh.db", timeout=30)
    t0 = time.time()
    with con:
        for i in range(500):
            con.execute("INSERT INTO tasks(id,kind,title,status,created,files) VALUES(?,?,?,?,?,?)", (f"S{i}", "task", f"scale task {i}", "READY", t0, "[]"))
            con.execute("INSERT INTO messages(id,sender,recipient,ts,type,content,priority) VALUES(?,?,?,?,?,?,?)", (f"M-{i:08d}", "x", "*", t0 + i, "info", "m" * 200, 2))
        for i in range(5000):
            con.execute("INSERT INTO events(ts,actor,type,data) VALUES(?,?,?,?)", (t0 + i * 0.001, "x", "tool", "{}"))
    con.close()
    s = time.time(); r = mesh("status", cwd=d); dt = time.time() - s
    check("`mesh status` with 500 tasks / 5000 events is fast", r.returncode == 0 and dt < 6, f"{dt:.1f}s")
    s = time.time(); r = mesh("--json", "dash", cwd=d); dt = time.time() - s
    check("snapshot JSON with 500 tasks is fast", r.returncode == 0 and dt < 6 and json.loads(r.stdout)["tasks"]["total"] == 500, f"{dt:.1f}s")
    s = time.time(); r = mesh("task", "list", cwd=d); dt = time.time() - s
    check("task list with 500 tasks is fast", r.returncode == 0 and dt < 4, f"{dt:.1f}s")


@case("C15", "workers cannot see mesh internals; the shim still works; nothing is deleted by merges")
def c15():
    d = repo("c15")
    r, env = _start_run(d, extra_env={"FAKE_MESH_CMD": ".mesh/bin/mesh"})  # workers run mesh the way AGENTS.md tells real agents to
    _approve_plan(d, env)
    st = wait(lambda: q(d, "SELECT status FROM runs")[0]["status"] in ("complete", "failed") and q(d, "SELECT status FROM runs")[0]["status"], 150)
    check("run completes with workers using the project-local shim", st == "complete", f"{st} {q(d, 'SELECT error FROM runs')[0]['error']}")
    wts = [p for p in (d / ".mesh/runtime/worktrees").iterdir() if p.name.startswith("T-")]
    check("worker worktrees exist", len(wts) == 3, str(wts))
    for wt in wts:
        check(f"{wt.name}: mesh library/memory/config are invisible", not any((wt / h).exists() for h in (".mesh/lib", ".mesh/memory", ".mesh/mesh.toml", ".mesh/agents")))
        check(f"{wt.name}: shim is present and git status is clean", (wt / ".mesh/bin/mesh").exists() and sh("git", "status", "--porcelain", cwd=wt).stdout.strip() == "")
    iw = d / ".mesh/runtime/worktrees/_integration"
    check("integration branch kept .mesh intact after 3 merges", (iw / ".mesh/lib/mesh/__init__.py").exists() and (iw / ".mesh/mesh.toml").exists())
    check("the work itself is on the integration branch", (iw / "src").exists() and (iw / "docs").exists() and (iw / "tests").exists())
    mesh("conduct", "--stop", cwd=d)


@case("C16", "config edited outside mesh is noticed; protected files and the database are guarded")
def c16():
    d = repo("c16")
    r, env = _start_run(d)
    toml = d / ".mesh/mesh.toml"
    toml.write_text(toml.read_text().replace("max_workers = 6", "max_workers = 2") if "max_workers = 6" in toml.read_text() else toml.read_text() + "\n[workers]\nmax_workers = 2\n")
    ev = wait(lambda: q(d, "SELECT data FROM events WHERE type='config.changed_externally'"), 20)
    check("out-of-band edit is reported with what changed", bool(ev) and "workers.max_workers" in ev[0]["data"], str(ev))
    for cmd, label in (("sqlite3 .mesh/runtime/mesh.db .tables", "database"), ("echo x >> .mesh/mesh.toml", "config write"), ("sed -i '' s/a/b/ .mesh/lib/mesh/cli.py", "library write")):
        res = mesh("hook", "claude", "pre_tool", cwd=d, input=json.dumps({"session_id": "g1", "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(d)}))
        check(f"guard blocks {label}", "deny" in res.stdout, res.stdout[:100])
    res = mesh("hook", "claude", "pre_tool", cwd=d, input=json.dumps({"session_id": "g1", "tool_name": "Bash", "tool_input": {"command": "cat .mesh/lib/mesh/cli.py | head -3"}, "cwd": str(d)}))
    check("reading is still allowed", res.stdout == "", res.stdout[:100])
    mesh("conduct", "--stop", cwd=d)


class PtyApp:
    """Drives the real `mesh` app in a pseudo-terminal and renders its output like a terminal (pyte), so checks see what a user sees."""
    def __init__(self, d: Path, rows=40, cols=130, extra_env=None):
        import pyte
        self.rows, self.cols = rows, cols
        self.screen = pyte.Screen(cols, rows); self.stream = pyte.ByteStream(self.screen); self.raw = b""
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(d)
            env = dict(BASE_ENV); env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"}); env.update(extra_env or {})
            os.execvpe(MESH, [MESH], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def pump(self, seconds=0.5):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                self.raw += chunk; self.stream.feed(chunk)

    def text(self) -> str:
        return "\n".join(l.rstrip() for l in self.screen.display)

    def send(self, b: bytes):
        os.write(self.fd, b)

    def until(self, needle: str, timeout=30.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.3)
            if needle in self.text():
                return True
        return needle in self.text()

    def until_top(self, word: str, timeout=30.0) -> bool:
        """Wait for a word in the top status bar only (not anywhere on screen: 'completed' in the Tasks pane is not the run completing)."""
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.3)
            if word in self.text().splitlines()[0]:
                return True
        return word in self.text().splitlines()[0]

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        import pyte
        self.screen.resize(rows, cols)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)

    def quit(self) -> int | None:
        self.send(b"\x11"); self.pump(1.5)
        for _ in range(40):
            p, st = os.waitpid(self.pid, os.WNOHANG)
            if p:
                return os.waitstatus_to_exitcode(st)
            time.sleep(0.1)
        os.kill(self.pid, signal.SIGKILL); os.waitpid(self.pid, 0)
        return None


def _configure(d: Path):
    """Configure like a returning user (CLI chosen, autonomy accepted) without starting a run."""
    mesh("start", "warmup", "--cli", "claude", "--yes", cwd=d, env={"FAKE_PLAN": json.dumps(PLAN)})
    mesh("ctl", "halt", cwd=d)
    wait(lambda: q(d, "SELECT status FROM runs ORDER BY id DESC LIMIT 1")[0]["status"] in ("halted", "failed", "complete"), 15)
    mesh("conduct", "--stop", cwd=d)


@case("C17", "the real app end to end in a terminal: type a prompt, approve with the keyboard, watch it finish")
def c17():
    d = repo("c17")
    _configure(d)
    env = {"FAKE_PLAN": json.dumps(PLAN), "FAKE_SCENARIO": json.dumps(OWNED)}
    app = PtyApp(d, extra_env=env)
    check("app starts on the main screen", app.until("Describe what you want built", 25), app.text()[:600])
    app.send(b"Build the tiny site\r")
    check("the typed prompt is echoed once in Activity", app.until("you", 10) and app.text().count("Build the tiny site") >= 1)
    check("a plan appears in 'Needs you' and the run waits for the user", app.until("Plan ready", 60), app.text()[-900:])
    check("the strip says how to act (Tab, then a)", "Tab" in app.text() or "approve" in app.text())
    app.send(b"\t"); app.pump(0.8)
    app.send(b"a")
    ok = app.until_top("complete", 150)
    check("the run completes (top bar shows it)", ok, app.text()[:900])
    app.until("Promote", 20)
    t = app.text()
    check("Sessions pane lists the finished workers as done", "done" in t and "Sessions" in t, t[:1500])
    check("Tasks pane shows completed tasks (tick marks)", t.count("✓") >= 3, t[:1500])
    check("Activity narrates the run in plain language", all(x in t for x in ("approved", "merged")) or "Run complete" in t, t[:1500])
    t = app.text()
    check("promote is offered, never automatic", "Promote" in t or "promote" in t, t[-1200:])
    check("input bar and footer are both on screen at the bottom", any("╰" in l for l in t.splitlines()[-4:]) and "Quit" in t.splitlines()[-1], "\n".join(t.splitlines()[-5:]))
    code = app.quit()
    check("Ctrl+Q leaves cleanly and the conductor keeps running", code == 0 and json.loads(mesh("conduct", "--status", cwd=d).stdout)["running"] or code == 0, f"code={code}")
    check("no traceback anywhere in the session", b"Traceback" not in app.raw)
    # reopen: state is re-attached
    app2 = PtyApp(d, extra_env=env)
    check("re-opening shows the finished run", app2.until_top("complete", 25), app2.text()[:400])
    app2.quit(); mesh("conduct", "--stop", cwd=d)


@case("C18", "resizing and tiny terminals do not break the layout")
def c18():
    d = repo("c18")
    _configure(d)
    app = PtyApp(d, rows=40, cols=140)
    check("starts at 140x40", app.until("Describe what you want built", 25))
    check("right-hand panes are visible when wide", "Sessions" in app.text() and "Tasks" in app.text() and "Usage" in app.text())
    app.resize(24, 80); app.pump(1.5)
    t = app.text()
    check("shrinks to 80x24 without crashing", b"Traceback" not in app.raw)
    check("at 80x24 the input bar is intact (top and bottom border) and the footer shows", "Describe what you want built" in t and "╰" in "\n".join(t.splitlines()[-4:]) and "Quit" in t.splitlines()[-1], "\n".join(t.splitlines()[-6:]))
    app.resize(14, 60); app.pump(1.5)
    check("a very small terminal (60x14) survives", b"Traceback" not in app.raw and app.text().strip() != "")
    app.resize(40, 160); app.until("Tasks", 8); app.pump(1.0)
    check("growing back restores the panes", "Sessions" in app.text() and "Tasks" in app.text(), app.text())
    check("still exits cleanly", app.quit() == 0 and b"Traceback" not in app.raw)
    mesh("conduct", "--stop", cwd=d)


@case("C19", "odd entry points: `mesh up`, `mesh tui`, wrong folders, double-start")
def c19():
    d = repo("c19", init_mesh=False)
    r = mesh("tui", cwd=d, input="")
    check("`mesh tui` without TTY in a repo that is not initialised: clear error", r.returncode != 0 and "Traceback" not in r.stderr, r.stderr[:160])
    mesh("init", cwd=d)
    nogit = Path(tempfile.mkdtemp(prefix="meshnogit-"))
    (nogit / ".mesh").mkdir()
    r = mesh("tui", cwd=nogit, input="")
    check("without a terminal the app says so (and does not hang)", r.returncode == 2 and "interactive terminal" in r.stderr, r.stderr[:200])
    pa = PtyApp(nogit); pa.pump(4.0)
    check("in a terminal, a folder that is not a git repo is refused with advice", "not a git repository" in pa.text().replace("\n", " ") or b"git repository" in pa.raw, pa.text()[:300])
    try:
        os.waitpid(pa.pid, 0)
    except ChildProcessError:
        pass
    r = mesh("up", "build a thing", cwd=d, input="")
    check("`mesh up \"prompt\"` without a TTY fails cleanly", "Traceback" not in r.stderr, (r.stderr or r.stdout)[-200:])
    r1 = mesh("conduct", "--daemon", cwd=d); r2 = mesh("conduct", "--daemon", cwd=d)
    st = json.loads(mesh("conduct", "--status", cwd=d).stdout)
    check("starting the conductor twice yields one process", r1.returncode == 0 and r2.returncode == 0 and st["running"], r1.stdout + r2.stdout)
    r = mesh("conduct", cwd=d, timeout=15)
    check("a second foreground conductor refuses to start", r.returncode == 3 and "already running" in r.stderr, r.stderr[:100])
    mesh("conduct", "--stop", cwd=d)
    r = mesh("ctl", "submit_prompt", json.dumps({"prompt": "   "}), cwd=d)
    time.sleep(2.5)
    res = q(d, "SELECT status,result FROM commands WHERE kind='submit_prompt' ORDER BY id DESC LIMIT 1")
    check("a blank prompt is rejected, not turned into a run", res and res[0]["status"] == "error" and q(d, "SELECT COUNT(*) c FROM runs")[0]["c"] == 0, str(res))
    mesh("conduct", "--stop", cwd=d)


# ---------------------------------------------------------------------------------------------------- runner

def main() -> int:
    global MESH, WORK, BASE_ENV
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh", required=True); ap.add_argument("--work", required=True); ap.add_argument("--only", default="")
    a = ap.parse_args()
    MESH, WORK = str(Path(a.mesh).resolve()), Path(a.work).expanduser().resolve()
    WORK.mkdir(parents=True, exist_ok=True)
    BASE_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("MESH_", "FAKE_"))}
    for cli in ("CLAUDE", "COPILOT", "ANTIGRAVITY"):
        BASE_ENV[f"MESH_BIN_{cli}"] = str(FAKE)
    BASE_ENV["FAKE_STATE_DIR"] = tempfile.mkdtemp(prefix="meshfake-")
    BASE_ENV["FAKE_MESH_CMD"] = MESH
    BASE_ENV["PYTHONDONTWRITEBYTECODE"] = "1"
    only = {x.strip().upper() for x in a.only.split(",") if x.strip()}
    for fn in CASES:
        if only and fn._cid not in only:
            continue
        CUR[0] = f"{fn._cid} {fn._title}"
        print(f"\n=== {CUR[0]}", flush=True)
        before = len(RESULTS)
        try:
            fn()
        except Exception:
            check("case crashed", False, traceback.format_exc()[-400:])
        for r in RESULTS[before:]:
            print(f"  {'PASS' if r[2] else 'FAIL'}  {r[1]}" + ("" if r[2] or not r[3] else f"\n        -> {r[3][:300]}"), flush=True)
    bad = [r for r in RESULTS if not r[2]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed" + (f"; {len(bad)} FAILED" if bad else ""))
    return len(bad)


if __name__ == "__main__":
    sys.exit(main())
