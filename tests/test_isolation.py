"""Findings from inspecting a REAL run: agents burned a third of their tool calls reading mesh's own source, queried the coordination
database directly, and the planner/brain explored mesh internals. These tests pin the fixes."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import MeshCase, Repo, sh
from test_workers import WorkerCase, wait_for
from test_conductor import CondCase, PLAN, OWNED
from mesh import brain, conductor, core, feed, gitflow, hooks, runs, safety, tasks, verify, workers
from mesh.db import Paths


def committed_project(files=None, **kw):
    r = Repo(files=files or {"README.md": "hi\n"}, **kw)
    r.mesh("sync"); r.mesh("commit-config")
    return r


class TestSparseWorktrees(WorkerCase):
    def test_workers_do_not_see_mesh_internals_but_can_still_run_mesh_and_commit_safely(self):
        r = self.proj(); db = r.db()
        t = tasks.create(db, "feature", files=["feat/**"], verify_cmd="true")
        w = workers.spawn(db, t, cli="claude", start=False)
        wt = Path(w["worktree"])
        for hidden in (".mesh/lib", ".mesh/memory", ".mesh/agents", ".mesh/instructions", ".mesh/mesh.toml"):
            self.assertFalse((wt / hidden).exists(), f"{hidden} must be invisible to the worker")
        self.assertTrue((wt / ".mesh/bin/mesh").exists())  # the shim stays (AGENTS.md tells agents to use it)
        self.assertTrue((wt / "AGENTS.md").exists())
        self.assertEqual(r.git("status", "--porcelain", cwd=wt).stdout.strip(), "")  # hidden is not "deleted"
        # the shim still works from inside the worktree through $MESH_ROOT
        env = {**r.env(), "MESH_ROOT": str(r.root)}; env.pop("PYTHONPATH", None)
        out = sh(str(wt / ".mesh/bin/mesh"), "task", "list", cwd=wt, env=env)
        self.assertEqual(out.returncode, 0, out.stderr); self.assertIn(t, out.stdout)
        # work, commit with `git add -A`, integrate: .mesh must survive intact in the integration branch
        tasks.transition(db, t, "IN_PROGRESS", w["id"])
        (wt / "feat").mkdir(); (wt / "feat" / "a.txt").write_text("x")
        r.git("add", "-A", cwd=wt); r.git("commit", "-qm", "w", cwd=wt)
        self.assertEqual(r.git("ls-tree", "-r", "--name-only", "HEAD", cwd=wt).stdout.count(".mesh/lib/"), r.git("ls-tree", "-r", "--name-only", "main").stdout.count(".mesh/lib/"))
        verify.submit(db, t, w["id"], ""); verify.verify(db, t, "orch"); verify.approve(db, t, "orch")
        self.assertTrue(gitflow.integrate(db, t, "orch")["ok"])
        iw = db.paths.worktrees / "_integration"
        self.assertTrue((iw / ".mesh/lib/mesh/__init__.py").exists() and (iw / "feat/a.txt").exists())  # nothing was deleted by the merge

    def test_real_worker_process_runs_mesh_via_the_shim_inside_a_sparse_worktree(self):
        r = self.proj(); db = r.db()
        os.environ["FAKE_MESH_CMD"] = ".mesh/bin/mesh"  # what AGENTS.md tells a real agent to run
        self.scenario(OWNED)
        t = tasks.create(db, "shim worker", files=["s/**"])
        w = workers.spawn(db, t, cli="claude")
        self.assertTrue(self.done(r, w["id"]))
        self.assertEqual(workers.get(db, w["id"])["state"], "COMPLETED")
        self.assertEqual(tasks.get(db, t)["status"], "REVIEW")

    def test_old_projects_whose_shim_cannot_find_lib_keep_it_visible(self):
        r = self.proj(); db = r.db()
        (r.root / ".mesh/bin/mesh").write_text('#!/bin/sh\nD="$(cd "$(dirname "$0")" && pwd)"\nPYTHONPATH="$D/../lib" exec python3 -m mesh "$@"\n')  # pre-0.3.2 shim
        r.mesh("commit-config")
        t = tasks.create(db, "old", files=["o/**"]); w = workers.spawn(db, t, cli="claude", start=False)
        self.assertTrue((Path(w["worktree"]) / ".mesh/lib/mesh/__init__.py").exists())  # compatibility: never break a project that has not upgraded


class TestShellGuards(MeshCase):
    def pre(self, r, cmd):
        out, _ = hooks.handle("claude", "pre_tool", {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(r.root)}, r.root)
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out else None

    def test_database_and_protected_files_are_off_limits_to_shell_commands_but_reading_code_is_fine(self):
        r = self.repo()
        for cmd in ('sqlite3 .mesh/runtime/mesh.db "select * from adrs"', "cat .mesh/runtime/mesh.db | head", "echo x > .mesh/mesh.toml", "sed -i s/a/b/ .mesh/mesh.toml",
                    "python3 -c \"open('.mesh/mesh.toml','w')\"", "cp /tmp/x .mesh/lib/mesh/verify.py", "rm -rf .mesh/lib"):
            self.assertIsNotNone(self.pre(r, cmd), cmd)
        for cmd in ("cat .mesh/mesh.toml", "ls .mesh/bin", ".mesh/bin/mesh task list", "python3 -m unittest discover -s tests", "echo hi > src/a.py", "rm -rf node_modules"):
            self.assertIsNone(self.pre(r, cmd), cmd)
        self.assertIn("mesh database", self.pre(r, "sqlite3 x/mesh.db .tables"))


class TestSandboxScrub(MeshCase):
    def test_planner_sandbox_has_no_mesh_tooling_or_protocol_but_keeps_user_instructions(self):
        r = Repo(files={"AGENTS.md": "# My team rules\nuse tabs\n", "src/a.py": "x=1\n"}, init=False)
        r.mesh("init"); r.mesh("commit-config")
        self.assertIn("mesh:begin", (r.root / "AGENTS.md").read_text())
        db = r.db()
        sb = brain.sandbox(db, "plan")
        self.assertFalse((sb / ".mesh").exists())
        txt = (sb / "AGENTS.md").read_text()
        self.assertIn("use tabs", txt); self.assertNotIn("mesh:begin", txt); self.assertNotIn("claim", txt.lower())
        self.assertFalse((sb / "CLAUDE.md").exists())  # it only held the managed import
        self.assertTrue((sb / "src/a.py").exists())
        sb2 = brain.sandbox(db, "plan")  # refresh is idempotent and re-scrubs
        self.assertFalse((sb2 / ".mesh").exists())
        self.assertIn("mesh:begin", (r.root / "AGENTS.md").read_text())  # the real project is untouched
        self.assertTrue((r.root / ".mesh/lib").exists())

    def test_claude_hooks_work_from_a_sandbox_that_has_no_local_mesh(self):
        r = committed_project(); db = r.db()
        sb = brain.sandbox(db, "plan")
        s = json.loads((r.root / ".claude/settings.json").read_text())
        cmd = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        env = {k: v for k, v in r.env().items() if k != "PYTHONPATH"}
        payload = json.dumps({"session_id": "sb1", "tool_name": "Bash", "tool_input": {"command": "git push --force"}, "cwd": str(sb)})
        res = sh("sh", "-c", cmd, cwd=sb, env=env, input=payload)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("deny", res.stdout)  # the guard still protects the sandbox through the main project's shim


class TestBrainWakeups(CondCase):
    def _run(self):
        r = self.project(); db = self.cond.db
        self.cmd("submit_prompt", prompt="x", cli="claude"); self.run_until(lambda: self.status() == "awaiting_approval")
        self.cmd("approve_item", id=runs.open_attention(db)[0]["id"])
        self.run_until(lambda: self.status() == "executing")
        return db

    def test_mesh_generated_notices_do_not_wake_the_brain_but_a_workers_blocker_does(self):
        from mesh import bus
        db = self._run()
        bus.send(db, "w-x", "role:orchestrator", "blocker", "worker w-x exited without submitting", None, 1, requires_ack=True, dedupe_key="unsub-w-x")
        bus.send(db, "w-x", "role:orchestrator", "blocker", "worker w-x has produced no output", None, 2, dedupe_key="stall-w-x-1")
        for _ in range(8):
            self.cond.tick(); time.sleep(0.2)
        self.assertEqual(self.calls("brain"), 0)  # nothing to decide: the conductor already handles these
        self.assertEqual(db.one("SELECT COUNT(*) c FROM messages WHERE dedupe_key IN ('unsub-w-x','stall-w-x-1') AND acked=1")["c"], 2)  # and they do not linger as pending
        os.environ["FAKE_BRAIN"] = json.dumps({"actions": []})
        bus.send(db, "w-y", "role:orchestrator", "blocker", "I need the API key name", None, 1, requires_ack=True)
        self.assertTrue(self.run_until(lambda: self.calls("brain") >= 1, 30))

    def test_brain_timeout_keeps_partial_output_and_says_where(self):
        r = self.project(); db = self.cond.db
        core.register_session(db, "claude", "orchestrator", None, os.getpid(), None, role="orchestrator", session_id="orch-9")
        real = subprocess.run
        def fake(cmd, **kw):
            if cmd and cmd[0] == "git":
                return real(cmd, **kw)
            raise subprocess.TimeoutExpired(cmd, 1, output=b'{"event":"init"}\n', stderr=b"slow")
        with mock.patch.object(brain.subprocess, "run", fake):
            res = brain.call(db, "claude", "p", brain.ACTION_SCHEMA, None, 1, "brain-9")
        self.assertIn("did not answer", res.error); self.assertIn("partial output saved", res.error)
        self.assertIn("init", (db.paths.runtime / "runs" / "brain-9" / "last.stdout.jsonl").read_text())

    def test_worker_prompt_tells_agents_to_stay_out_of_mesh_internals(self):
        r = self.project(); db = self.cond.db
        t = tasks.create(db, "p", files=["p/**"]); w = workers.spawn(db, t, cli="claude", start=False)
        prompt = (Path(w["run_dir"]) / "prompt.md").read_text()
        for needle in ("Stay on your task", "ignore the generic multi-agent protocol", "never query the database directly"):
            self.assertIn(needle, prompt)


class TestConfigDrift(CondCase):
    def test_edits_to_mesh_toml_outside_mesh_are_noticed_once_and_explained(self):
        r = self.project(); db = self.cond.db
        core.set_toml_section(self.paths.config, "validation", {"test": "npm test", "lint": ""})
        self.cond.tick()
        self.assertFalse(db.q("SELECT 1 FROM events WHERE type='config.changed_externally'"))  # mesh's own writes are not "drift"
        text = self.paths.config.read_text().replace('test = "npm test"', 'test = ""')
        self.paths.config.write_text(text)  # someone (a person or an agent) edits it by hand
        self.cond.tick(); self.cond.tick()
        ev = db.q("SELECT data FROM events WHERE type='config.changed_externally'")
        self.assertEqual(len(ev), 1)  # reported once per change
        self.assertIn("validation.test", ev[0]["data"]); self.assertIn("npm test", ev[0]["data"])
        line = next(i["text"] for i in feed.activity(self.paths) if "edited outside mesh" in i["text"])
        self.assertIn("validation.test", line)


if __name__ == "__main__":
    unittest.main()
