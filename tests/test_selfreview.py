"""Regressions for problems found by inspecting a REAL run (workers looping because project tests did not exist yet,
phantom planner session, over-strict rm guard) plus polish items from real screenshots."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import MeshCase, sh
from test_workers import WorkerCase, wait_for
from test_conductor import CondCase, OWNED
from mesh import brain, conductor, core, gitflow, runs, safety, snapshot, tasks, verify, workers
from mesh.db import MeshError, Paths


class TestRmGuard(unittest.TestCase):
    def test_disposable_artefacts_allowed_everything_dangerous_blocked(self):
        ok = ["rm -rf src/__pycache__", "rm -rf node_modules dist", "ls && rm -rf build", "rm -rf src/**/__pycache__", "rm -rf .pytest_cache", "rm -rf a/b/*.pyc",
              "rm -fr .next", "rm -r -f target"]
        bad = ["rm -rf /", "rm -rf ~", "rm -rf .", "rm -rf *", "rm -rf ../x", "rm -rf src", "rm -rf $HOME/x", "rm -rf build; rm -rf /tmp/x",
               "rm -rf /build", "rm -rf ~/build", "rm -rf ./..", "rm -rf node_modules ../..", "sudo rm -rf /var", "rm -rf .mesh", "rm -rf .git"]
        for c in ok:
            self.assertIsNone(safety.check_command(c), c)
        for c in bad:
            self.assertIsNotNone(safety.check_command(c), c)


class TestBaselineAwareValidation(WorkerCase):
    def test_REGRESSION_project_tests_that_do_not_exist_yet_do_not_block_earlier_tasks(self):
        """Seen live: validation `unittest discover -s tests` failed on every task branch because the QA task creates tests/ later."""
        r = self.proj(test_cmd="test -d tests"); db = r.db()
        a = tasks.create(db, "impl", files=["src/**"], verify_cmd="test -f src/x.py")
        w = workers.spawn(db, a, cli="claude", start=False)
        tasks.transition(db, a, "IN_PROGRESS", w["id"])
        wt = __import__("pathlib").Path(w["worktree"]); (wt / "src").mkdir(); (wt / "src" / "x.py").write_text("x=1\n")
        r.git("add", "-A", cwd=wt); r.git("commit", "-qm", "impl", cwd=wt)
        verify.submit(db, a, w["id"], "")
        v = verify.verify(db, a, "orch")
        self.assertTrue(v["ok"], v["detail"])  # its own contract passes; project tests are not applied per task
        self.assertIn("not run per task", v["detail"])
        verify.approve(db, a, "orch")
        res = gitflow.integrate(db, a, "orch")
        self.assertTrue(res["ok"], res)  # baseline was already red (no tests/), so it is not blamed on this task
        self.assertTrue(db.q("SELECT 1 FROM events WHERE type='integrate.baseline_red'"))
        # the QA task then adds tests/: validation turns green on the integration branch
        b = tasks.create(db, "tests", files=["tests/**"], verify_cmd="test -d tests", deps=[a])
        w2 = workers.spawn(db, b, cli="claude", start=False); tasks.transition(db, b, "IN_PROGRESS", w2["id"])
        wt2 = __import__("pathlib").Path(w2["worktree"]); (wt2 / "tests").mkdir(); (wt2 / "tests" / "t.py").write_text("ok\n")
        r.git("add", "-A", cwd=wt2); r.git("commit", "-qm", "tests", cwd=wt2)
        verify.submit(db, b, w2["id"], ""); verify.verify(db, b, "orch"); verify.approve(db, b, "orch")
        self.assertTrue(gitflow.integrate(db, b, "orch")["ok"])

    def test_a_task_that_BREAKS_a_green_baseline_is_still_blocked_and_reverted(self):
        r = self.proj(test_cmd="test ! -e BROKEN"); db = r.db()
        t = tasks.create(db, "bad", files=["x/**"], verify_cmd="true")
        w = workers.spawn(db, t, cli="claude", start=False); tasks.transition(db, t, "IN_PROGRESS", w["id"])
        wt = __import__("pathlib").Path(w["worktree"]); (wt / "BROKEN").write_text("1")
        r.git("add", "-A", cwd=wt); r.git("commit", "-qm", "break", cwd=wt)
        res = gitflow.integrate(db, t, "orch")
        self.assertEqual(res["stage"], "validate"); self.assertEqual(tasks.get(db, t)["status"], "NEEDS_REWORK")

    def test_task_without_its_own_verify_uses_project_tests_but_red_baseline_is_weak_not_failed(self):
        r = self.proj(test_cmd="test -d tests"); db = r.db()
        t = tasks.create(db, "nocontract", files=["a/**"])
        w = workers.spawn(db, t, cli="claude", start=False); tasks.transition(db, t, "IN_PROGRESS", w["id"])
        wt = __import__("pathlib").Path(w["worktree"]); (wt / "a").mkdir(); (wt / "a" / "f").write_text("1")
        r.git("add", "-A", cwd=wt); r.git("commit", "-qm", "w", cwd=wt)
        verify.submit(db, t, w["id"], "")
        v = verify.verify(db, t, "orch")
        self.assertTrue(v["ok"]); self.assertTrue(v["weak"])  # not blamed, but not strong evidence either => a human is asked
        self.assertIn("not blamed", v["detail"])
        with self.assertRaises(MeshError):
            verify.approve(db, t, "orch")  # weak needs --accept-unverified


class TestConductorRegression(CondCase):
    def test_REGRESSION_full_run_where_tests_dir_appears_only_in_the_last_task(self):
        plan = {"summary": "hello app", "validation_cmd": "test -d tests", "decisions": [], "assumptions": [],
                "epics": [{"title": "Core", "tasks": [
                    {"key": "impl", "title": "implement", "description": "d", "agent": "backend", "files": ["src/**"], "acceptance": "a", "verify_cmd": "true"},
                    {"key": "docs", "title": "docs", "description": "d", "agent": "docs", "files": ["docs/**"], "acceptance": "a", "verify_cmd": "true"},
                    {"key": "qa", "title": "tests", "description": "d", "agent": "qa", "files": ["tests/**"], "deps": ["impl"], "acceptance": "a", "verify_cmd": "true"}]}]}
        r = self.project(plan=plan, scenario=OWNED); db = self.cond.db
        core.set_toml_section(self.paths.config, "validation", {"test": "test -d tests", "lint": ""})
        self.cmd("submit_prompt", prompt="hello", cli="claude")
        self.run_until(lambda: self.status() == "awaiting_approval")
        self.cmd("approve_item", id=runs.open_attention(db)[0]["id"])
        self.assertTrue(self.run_until(lambda: self.status() in ("complete", "failed"), 150), self.status())
        self.assertEqual(self.status(), "complete", self.cur_run()["error"])
        self.assertEqual(db.one("SELECT COUNT(*) c FROM events WHERE type='task.rejected'")["c"], 0)  # nobody was sent back for a failure they did not cause
        self.assertIn("PASS", self.cur_run()["final_validation"])

    def test_orchestrator_shows_thinking_while_planning_and_planner_runs_attributed_to_it(self):
        r = self.project(); db = self.cond.db
        os.environ["FAKE_PLAN_DELAY"] = "2.5"
        self.cmd("submit_prompt", prompt="x", cli="claude")
        end = time.time() + 20; seen = None
        while time.time() < end:
            self.cond.tick()
            a = [x for x in snapshot.build(self.paths)["agents"] if x["kind"] == "orchestrator"]
            if a and a[0]["state"] == "THINKING":
                seen = a[0]; break
            time.sleep(0.2)
        self.assertIsNotNone(seen, "orchestrator never showed THINKING during planning")
        self.assertTrue(self.run_until(lambda: self.status() == "awaiting_approval", 30))
        self.assertEqual(db.one("SELECT COUNT(*) c FROM sessions WHERE role IS NULL")["c"], 0)  # no phantom interactive session

    def test_planner_and_brain_clis_run_with_the_orchestrators_session_and_utilities_with_nohook(self):
        r = self.project(); db = self.cond.db
        core.register_session(db, "claude", "orchestrator", None, os.getpid(), None, role="orchestrator", session_id="orch-7")
        seen = {}
        real_run = subprocess.run
        def fake_run(cmd, **kw):
            if cmd and cmd[0] == "git":
                return real_run(cmd, **kw)  # sandbox creation must still really work
            seen["env"] = kw["env"]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"type": "result", "is_error": False, "result": "{}", "structured_output": {}, "session_id": "s"}), stderr="")
        with mock.patch.object(brain.subprocess, "run", fake_run):
            brain.call(db, "claude", "p", brain.PLAN_SCHEMA, None, 5, "plan-7")
            self.assertEqual(seen["env"]["MESH_SESSION"], "orch-7"); self.assertNotIn("MESH_NOHOOK", seen["env"])
            brain.call(db, "claude", "p", brain.PLAN_SCHEMA, None, 5, "brain-7")
            self.assertEqual(seen["env"]["MESH_SESSION"], "orch-7")
            brain.call(db, "claude", "p", brain.PLAN_SCHEMA, None, 5, "limits-claude")
            self.assertEqual(seen["env"]["MESH_NOHOOK"], "1"); self.assertNotIn("MESH_SESSION", seen["env"])

    def test_nohook_runs_register_nothing_and_cost_nothing(self):
        r = self.repo(); db = r.db()
        env = {**r.env(), "MESH_NOHOOK": "1"}
        out = sh(sys.executable, "-m", "mesh", "hook", "antigravity", "session_start", cwd=r.root, env=env, input=json.dumps({"conversationId": "abc12345", "workspacePaths": [str(r.root)]}))
        self.assertEqual((out.returncode, out.stdout), (0, ""))
        self.assertEqual(db.one("SELECT COUNT(*) c FROM sessions")["c"], 0)


class TestCliBasics(MeshCase):
    def test_version_flag_and_doctor_health_section(self):
        r = self.repo()
        v = r.mesh("--version")
        self.assertEqual(v.returncode, 0); self.assertRegex(v.stdout, r"agent-mesh \d+\.\d+\.\d+")
        d = r.mesh("doctor")
        self.assertIn("== Health", d.stdout); self.assertIn("database schema v5", d.stdout); self.assertIn("mesh 0.", d.stdout)


if __name__ == "__main__":
    unittest.main()
