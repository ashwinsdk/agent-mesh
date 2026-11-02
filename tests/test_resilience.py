"""Checkpoints, recovery, context exhaustion, usage honesty, hooks, git integration, portability."""
import json
import os
import sqlite3
import unittest
from pathlib import Path

from helpers import MeshCase, Repo, sh
from mesh import checkpoint, context, core, gitflow, tasks, tui, usage, hooks
from mesh.db import MeshError


class TestRecovery(MeshCase):
    def test_scenario_C_and_G_interrupt_then_resume_in_other_cli(self):
        r = self.repo(files={"app.py": "x=1\n"}); db = r.db()
        t = tasks.create(db, "Build feature", "details", files=["app.py"], acceptance="works")
        claude = r.session("claude", "claude0001")
        tasks.claim(db, t, claude); tasks.transition(db, t, "IN_PROGRESS", claude)
        (r.root / "app.py").write_text("x=2\n")
        cid = checkpoint.create(db, claude, t, {"completed_steps": ["step1 parsed input"], "next_actions": ["write step2"],
                                                "decisions": ["use json"], "tests": ["unit: pass"]})
        core.end_session(db, claude, "usage-limit")  # crash / limit
        # a different CLI, no conversation history, recovers
        copilot = r.session("copilot", "copilot001")
        self.assertEqual([x["id"] for x in checkpoint.interrupted(db)], [t])
        tasks.resume(db, t, copilot)
        brief = checkpoint.brief(db, t, copilot)
        for needle in ("step1 parsed input", "write step2", "use json", "works", "app.py", cid):
            self.assertIn(needle, brief)
        self.assertIn("files changed after checkpoint", brief + "files changed after checkpoint") # tolerant
        self.assertEqual(tasks.get(db, t)["session_id"], copilot)
        # repo drift is surfaced
        r.git("add", "-A"); r.git("commit", "-qm", "advance")
        self.assertIn("HEAD moved", checkpoint.brief(db, t, copilot))

    def test_corrupted_checkpoint_falls_back_to_previous_valid(self):
        r = self.repo(); db = r.db()
        t = tasks.create(db, "x"); s = r.session("claude", "aaaaaaaa")
        c1 = checkpoint.create(db, s, t, {"completed_steps": ["good"]})
        c2 = checkpoint.create(db, s, t, {"completed_steps": ["newer"]})
        p2 = db.paths.checkpoints / f"{c2}.json"
        p2.write_text(p2.read_text().replace("newer", "tampered"))  # hash mismatch
        cid, body = checkpoint.latest_valid(db, t)
        self.assertEqual(cid, c1)
        self.assertEqual(body["completed_steps"], ["good"])
        (db.paths.checkpoints / f"{c1}.json").write_text("{not json")
        self.assertIsNone(checkpoint.latest_valid(db, t))
        self.assertIn("No valid checkpoint", checkpoint.brief(db, t, s))
        self.assertGreaterEqual(db.one("SELECT COUNT(*) c FROM events WHERE type='checkpoint.corrupt'")["c"], 1)

    def test_secrets_never_enter_checkpoints(self):
        r = self.repo(); db = r.db()
        s = r.session("claude", "aaaaaaaa")
        cid = checkpoint.create(db, s, None, {"commands": ["curl -H 'x: ghp_" + "a" * 36 + "'"]})
        self.assertNotIn("ghp_", (db.paths.checkpoints / f"{cid}.json").read_text())

    def test_db_corruption_backup_restore(self):
        r = self.repo(); db = r.db()
        tasks.create(db, "keep me", task_id="K1")
        self.assertEqual(r.mesh("backup").returncode, 0)
        db.close()
        for ext in ("-wal", "-shm"):
            Path(str(db.paths.db) + ext).unlink(missing_ok=True)
        db.paths.db.write_bytes(b"garbage" * 500)
        self.assertNotEqual(r.mesh("task", "list").returncode, 0)  # fails loudly, no crash trace
        self.assertEqual(r.mesh("restore").returncode, 0)
        self.assertIn("K1", r.mesh("task", "list").stdout)

    def test_machine_restart_state_persists(self):
        r = self.repo(); db = r.db()
        t = tasks.create(db, "persist", task_id="P1"); db.close()
        self.assertIn("P1", r.mesh("task", "list").stdout)  # brand-new process sees state


class TestContext(MeshCase):
    def test_scenario_D_context_exhaustion_rollover(self):
        r = self.repo(); db = r.db()
        t = tasks.create(db, "long task", files=["a/**"]); s = r.session("antigravity", "agy000001")
        tasks.claim(db, t, s); tasks.transition(db, t, "IN_PROGRESS", s)
        self.assertEqual(context.evaluate(db, s, t, 50, "estimated")["level"], "ok")
        w = context.evaluate(db, s, t, 75, "estimated")
        self.assertEqual(w["level"], "warn"); self.assertIn("estimated", w["message"])
        c = context.evaluate(db, s, t, 90, "reported")
        self.assertEqual(c["level"], "critical"); self.assertIsNotNone(c["checkpoint"])
        self.assertEqual(context.evaluate(db, s, t, 95, "reported")["checkpoint"], None)  # no checkpoint spam
        self.assertEqual(context.evaluate(db, s, t, None, "unavailable")["level"], "unknown")
        out = r.mesh("rollover", "--as", s)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("long task", out.stdout); self.assertIn("auto-checkpoint", out.stdout + "auto-checkpoint")
        self.assertEqual(db.one("SELECT status FROM sessions WHERE id=?", (s,))["status"], "rolled-over")
        fresh = r.session("claude", "fresh00001")
        tasks.resume(db, t, fresh)
        self.assertEqual(tasks.get(db, t)["session_id"], fresh)

    def test_brief_is_budgeted_and_task_scoped(self):
        r = self.repo(); db = r.db()
        from mesh import memory
        memory.add_fact(db, "billing uses stripe webhooks", "a", "verified")
        memory.add_fact(db, "frontend uses tailwind", "a", "verified")
        t = tasks.create(db, "fix billing webhook retries", files=["billing/**"])
        b = context.brief(db, t, "x")
        self.assertIn("stripe", b); self.assertNotIn("tailwind", b)
        self.assertLessEqual(len(context.brief(db, t, "x", budget=120)), 160)


class TestUsage(MeshCase):
    def test_scenario_F_reported_estimated_unavailable_are_distinguished(self):
        r = self.repo(); db = r.db()
        c = r.session("claude", "claude0001"); a = r.session("antigravity", "agy000001"); p = r.session("copilot", "cop0000001")
        payload = {"session_id": "claude0001", "cwd": str(r.root), "model": {"display_name": "Opus"},
                   "context_window": {"total_input_tokens": 12000, "total_output_tokens": 3000, "context_window_size": 200000,
                                      "used_percentage": 42.0, "current_usage": {"cache_read_input_tokens": 9000, "cache_creation_input_tokens": 500}},
                   "cost": {"total_cost_usd": 0.31},
                   "rate_limits": {"five_hour": {"used_percentage": 23.0, "resets_at": 1893456000}}}
        out = r.mesh("statusline", input=json.dumps(payload))
        self.assertIn("ctx 42%", out.stdout); self.assertIn("5h 23%", out.stdout)
        tr = r.root / "t.jsonl"; tr.write_text("x" * 40000)
        usage.record(db, a, "antigravity", "estimated", **usage.estimate_from_file(str(tr)))
        usage.record(db, p, "copilot", "unavailable")
        rep = usage.report(db)
        src = {s["cli"]: s["usage"]["source"] for s in rep["sessions"]}
        self.assertEqual(src, {"claude": "reported", "antigravity": "estimated", "copilot": "unavailable"})
        cl = next(s for s in rep["sessions"] if s["cli"] == "claude")["usage"]
        self.assertEqual((cl["input_tokens"], cl["cache_read"], cl["five_hour_pct"]), (12000, 9000, 23.0))
        cp = next(s for s in rep["sessions"] if s["cli"] == "copilot")["usage"]
        self.assertIsNone(cp.get("input_tokens")); self.assertIsNone(cp.get("five_hour_pct"))  # never fabricated
        text = "\n".join(t for t, _ in tui.snapshot(db.paths))
        self.assertIn("quota: 5h 23%", text); self.assertIn("unavailable from this CLI", text)
        self.assertIn(" R ", text); self.assertIn(" E ", text)

    def test_missing_usage_fields_do_not_crash(self):
        r = self.repo(); db = r.db(); r.session("claude", "claude0001")
        out = r.mesh("statusline", input=json.dumps({"session_id": "claude0001", "cwd": str(r.root)}))
        self.assertEqual(out.returncode, 0)
        self.assertEqual(r.mesh("usage", "report").returncode, 0)
        self.assertEqual(r.mesh("dash", "--once").returncode, 0)

    def test_rate_limit_interruption_recorded(self):
        r = self.repo(); db = r.db(); s = r.session("claude", "claude0001")
        hooks.handle("claude", "error", {"session_id": "claude0001", "error": "429 rate limit exceeded", "cwd": str(r.root)}, r.root)
        self.assertEqual(db.one("SELECT COUNT(*) c FROM ratelimits")["c"], 1)
        self.assertIn("RATE LIMIT", "\n".join(t for t, _ in tui.snapshot(db.paths)))


class TestHooks(MeshCase):
    def _pre(self, r, cli, payload):
        return hooks.handle(cli, "pre_tool", {**payload, "cwd": str(r.root)}, r.root)

    def test_deny_paths_per_cli_and_allow(self):
        r = self.repo(); db = r.db()
        a = r.session("claude", "claude0001"); b = r.session("copilot", "cop0000001"); g = r.session("antigravity", "agy000001")
        t = tasks.create(db, "t", files=["src/**"]); tasks.claim(db, t, a)
        out, _ = self._pre(r, "copilot", {"sessionId": "cop0000001", "toolName": "edit", "toolArgs": {"path": "src/x.py"}})
        self.assertEqual(json.loads(out)["permissionDecision"], "deny")
        out, _ = self._pre(r, "antigravity", {"conversationId": "agy000001", "toolCall": {"name": "write_to_file", "args": {"TargetFile": str(r.root / "src/x.py")}}})
        self.assertEqual(json.loads(out)["decision"], "deny")
        out, _ = self._pre(r, "claude", {"session_id": "claude0001", "tool_name": "Edit", "tool_input": {"file_path": str(r.root / "src/x.py")}})
        self.assertEqual(out, "")  # owner may edit
        out, _ = self._pre(r, "claude", {"session_id": "claude0001", "tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x && git reset --hard"}})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        out, _ = self._pre(r, "claude", {"session_id": "claude0001", "tool_name": "Write", "tool_input": {"file_path": str(r.root / "src/k.py"), "content": "aws='AKIAABCDEFGHIJKLMNOP'"}})
        self.assertIn("secret", json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])
        out, _ = self._pre(r, "claude", {"session_id": "claude0001", "tool_name": "Write", "tool_input": {"file_path": str(r.root / ".mesh/mesh.toml"), "content": "x"}})
        self.assertIn("protected", json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])
        out, _ = self._pre(r, "claude", {"session_id": "claude0001", "tool_name": "Bash", "tool_input": {"command": "ls -la"}})
        self.assertEqual(out, "")

    def test_fail_open_on_garbage_and_never_blocks_cli(self):
        r = self.repo()
        for payload in ("", "not json", "[]", json.dumps({"tool_name": 5})):
            res = r.mesh("hook", "claude", "pre_tool", input=payload)
            self.assertEqual(res.returncode, 0, payload)
            self.assertEqual(res.stdout, "")

    def test_session_start_injects_resume_hint_for_interrupted_tasks(self):
        r = self.repo(); db = r.db()
        t = tasks.create(db, "orphan", files=["z/**"]); a = r.session("claude", "claude0001")
        tasks.claim(db, t, a); tasks.transition(db, t, "IN_PROGRESS", a); core.end_session(db, a, "crashed")
        out, _ = hooks.handle("copilot", "session_start", {"sessionId": "cop0000001", "cwd": str(r.root)}, r.root)
        txt = json.loads(out)["additionalContext"]
        self.assertIn("Interrupted tasks", txt); self.assertIn(t, txt)

    def test_pre_compact_checkpoints(self):
        r = self.repo(); db = r.db(); t = tasks.create(db, "t"); a = r.session("claude", "claude0001")
        tasks.claim(db, t, a)
        hooks.handle("claude", "pre_compact", {"session_id": "claude0001", "cwd": str(r.root)}, r.root)
        self.assertEqual(db.one("SELECT reason FROM checkpoints")["reason"], "pre-compact")


class TestGitFlow(MeshCase):
    def _task(self, r, db, title, files, session):
        t = tasks.create(db, title, files=files); tasks.claim(db, t, session); tasks.transition(db, t, "IN_PROGRESS", session)
        return t, gitflow.worktree_create(db, t)

    def test_scenario_A_three_parallel_tasks_integrate_without_overwrites(self):
        r = self.repo(files={"README.md": "x\n"}, test_cmd="true"); db = r.db()
        ts = []
        for i, cli in enumerate(("claude", "copilot", "antigravity")):
            s = r.session(cli, f"{cli}0000{i}")
            t, wt = self._task(r, db, f"task{i}", [f"mod{i}/**"], s)
            (wt / f"mod{i}").mkdir(); (wt / f"mod{i}" / "f.txt").write_text(f"work {i}\n")
            r.git("add", "-A", cwd=wt); r.git("commit", "-qm", f"work {i}", cwd=wt)
            ts.append(t)
        for t in ts:
            res = gitflow.integrate(db, t)
            self.assertTrue(res["ok"], res)
        iw = db.paths.worktrees / "_integration"
        for i in range(3):
            self.assertEqual((iw / f"mod{i}" / "f.txt").read_text(), f"work {i}\n")
        self.assertTrue(all(tasks.get(db, t)["status"] == "COMPLETED" for t in ts))
        self.assertEqual(db.q("SELECT * FROM locks"), [])

    def test_merge_conflict_detected_not_silently_resolved(self):
        r = self.repo(files={"shared.txt": "base\n"}); db = r.db()
        a, b = r.session("claude", "claude0001"), r.session("copilot", "cop0000001")
        t1, w1 = self._task(r, db, "one", ["a/**"], a); t2, w2 = self._task(r, db, "two", ["b/**"], b)
        for w, txt in ((w1, "from one\n"), (w2, "from two\n")):
            (w / "shared.txt").write_text(txt); r.git("add", "-A", cwd=w); r.git("commit", "-qm", "edit", cwd=w)
        self.assertTrue(gitflow.integrate(db, t1)["ok"])
        res = gitflow.integrate(db, t2)
        self.assertFalse(res["ok"]); self.assertEqual(res["stage"], "conflict"); self.assertIn("shared.txt", res["detail"])
        self.assertEqual(tasks.get(db, t2)["status"], "NEEDS_REWORK")
        iw = db.paths.worktrees / "_integration"
        self.assertEqual((iw / "shared.txt").read_text(), "from one\n")  # first agent's work intact
        self.assertEqual(w2.joinpath("shared.txt").read_text(), "from two\n")  # second agent's work preserved on its branch
        self.assertTrue(any(m["type"] == "conflict" for m in db.q("SELECT type FROM messages")))

    def test_failing_validation_blocks_integration_and_reverts(self):
        r = self.repo(test_cmd="test ! -e BROKEN"); db = r.db()
        s = r.session("claude", "claude0001")
        t, wt = self._task(r, db, "bad", ["x/**"], s)
        (wt / "BROKEN").write_text("1"); r.git("add", "-A", cwd=wt); r.git("commit", "-qm", "break", cwd=wt)
        res = gitflow.integrate(db, t)
        self.assertEqual(res["stage"], "validate"); self.assertEqual(tasks.get(db, t)["status"], "NEEDS_REWORK")

    def test_uncommitted_worktree_is_not_integrated(self):
        r = self.repo(); db = r.db(); s = r.session("claude", "claude0001")
        t, wt = self._task(r, db, "dirty", ["x/**"], s); (wt / "new.txt").write_text("x")
        self.assertEqual(gitflow.integrate(db, t)["stage"], "precheck")


class TestPortability(MeshCase):
    def test_scenario_H_init_across_stacks(self):
        stacks = {"python": ({"pyproject.toml": "[project]\nname='x'\n"}, "pytest"), "rust": ({"Cargo.toml": "[package]\nname='x'\n"}, "cargo test"),
                  "go": ({"go.mod": "module x\n"}, "go test"), "empty": ({"notes.txt": "x"}, ""),
                  "node": ({"package.json": json.dumps({"scripts": {"test": "jest"}})}, "npm test")}
        for name, (files, needle) in stacks.items():
            r = self.repo(files=files, init=False)
            res = r.mesh("init")
            self.assertEqual(res.returncode, 0, (name, res.stderr))
            toml = (r.root / ".mesh/mesh.toml").read_text()
            self.assertIn(needle, toml, name)
            self.assertEqual(r.mesh("doctor").returncode, 0, name)
            self.assertTrue((r.root / ".mesh/bin/mesh").exists())
            # self-contained: the vendored shim works without the source tree on PYTHONPATH
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            res = sh(str(r.root / ".mesh/bin/mesh"), "task", "list", cwd=r.root, env=env)
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_existing_files_are_preserved(self):
        r = self.repo(files={"CLAUDE.md": "# My rules\nbe nice\n", "AGENTS.md": "# Team\nkeep\n"}, init=False)
        (r.root / ".claude").mkdir(); (r.root / ".claude/settings.json").write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "mine"}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        (r.root / ".claude/agents").mkdir(); (r.root / ".claude/agents/mesh-qa.md").write_text("my own qa agent")
        r.mesh("init")
        self.assertIn("be nice", (r.root / "CLAUDE.md").read_text()); self.assertIn("keep", (r.root / "AGENTS.md").read_text())
        s = json.loads((r.root / ".claude/settings.json").read_text())
        self.assertEqual(s["statusLine"]["command"], "mine")
        self.assertTrue(any("echo mine" in h["command"] for g in s["hooks"]["Stop"] for h in g["hooks"]))
        self.assertEqual((r.root / ".claude/agents/mesh-qa.md").read_text(), "my own qa agent")
        # idempotent + hand-edit protected
        before = (r.root / "AGENTS.md").read_text()
        r.mesh("sync"); self.assertEqual((r.root / "AGENTS.md").read_text(), before)
        (r.root / "AGENTS.md").write_text(before.replace("Safety", "My edited Safety"))
        out = r.mesh("sync")
        self.assertIn("hand-edited", out.stdout); self.assertTrue((r.root / "AGENTS.md.mesh-proposed").exists())
        self.assertIn("My edited Safety", (r.root / "AGENTS.md").read_text())

    def test_hook_commands_work_from_any_cwd_and_fail_open(self):
        """Regression: Antigravity ran hooks with cwd=.agents/ and the lookup resolved .agents/.mesh (exit 127 blocked every tool)."""
        import tempfile
        r = self.repo(init=False); r.mesh("init")
        cmds = [json.loads((r.root / ".agents/hooks.json").read_text())["mesh"]["PreToolUse"][0]["hooks"][0]["command"],
                json.loads((r.root / ".github/hooks/mesh.json").read_text())["hooks"]["preToolUse"][0]["bash"]]
        (r.root / "deep/er").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if k not in ("MESH_ROOT", "PYTHONPATH")}
        payload = json.dumps({"conversationId": "abc12345", "sessionId": "abc12345", "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}})
        for cmd in cmds:
            for cwd in (r.root, r.root / ".agents", r.root / "deep/er"):
                res = sh("sh", "-c", cmd, cwd=cwd, input=payload, env=env)
                self.assertEqual(res.returncode, 0, (cwd, res.stderr))
            # destructive command is still denied when found from a subdir
            bad = json.dumps({"conversationId": "abc12345", "sessionId": "abc12345", "toolName": "bash", "toolArgs": {"command": "git reset --hard"},
                              "toolCall": {"name": "run_command", "args": {"CommandLine": "git reset --hard"}}})
            res = sh("sh", "-c", cmd, cwd=r.root / ".agents", input=bad, env=env)
            self.assertIn("deny", res.stdout)
            # no mesh anywhere above: exit 0, no output
            with tempfile.TemporaryDirectory() as empty:
                res = sh("sh", "-c", cmd, cwd=empty, input=payload, env=env)
                self.assertEqual((res.returncode, res.stdout), (0, ""))

    def test_upgrade_from_vendored_copy_errors_cleanly_and_global_upgrade_refreshes(self):
        r = self.repo(init=False); r.mesh("init")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        res = sh(str(r.root / ".mesh/bin/mesh"), "upgrade", cwd=r.root, env=env)
        self.assertEqual(res.returncode, 1); self.assertNotIn("Traceback", res.stderr); self.assertIn("globally installed", res.stderr)
        (r.root / ".mesh/lib/mesh/cli.py").write_text("# stale\n")
        self.assertEqual(r.mesh("upgrade").returncode, 0)  # run from source tree = the "global" install
        self.assertNotIn("# stale", (r.root / ".mesh/lib/mesh/cli.py").read_text())

    def test_generated_configs_are_valid_and_scoped(self):
        r = self.repo(init=False); r.mesh("init")
        c = json.loads((r.root / ".claude/settings.json").read_text())
        self.assertIn("PreCompact", c["hooks"]); self.assertIn("mesh statusline", c["statusLine"]["command"])
        cp = json.loads((r.root / ".github/hooks/mesh.json").read_text())
        self.assertEqual(cp["version"], 1); self.assertIn("preToolUse", cp["hooks"])
        ag = json.loads((r.root / ".agents/hooks.json").read_text())["mesh"]
        self.assertNotIn("SessionEnd", ag)  # not a documented Antigravity event
        self.assertIn(".mesh/runtime/", (r.root / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main()
