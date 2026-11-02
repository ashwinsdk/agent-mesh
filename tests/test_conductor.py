"""The conductor drives a run from prompt to completion with NO LLM executing the process.
Planner and brain are fake CLIs returning structured JSON; workers are real runner processes driving the fake CLI."""
import json
import os
import signal
import sys
import time
import unittest
from pathlib import Path

from helpers import MeshCase
from test_workers import WorkerCase, wait_for
from mesh import bus, conductor, core, runs, tasks, usage, verify, workers
from mesh.db import DB, MeshError, Paths

PLAN = {
    "summary": "One Piece 3D site", "validation_cmd": "true",
    "decisions": [{"title": "Use Three.js with Vite", "context": "3D in the browser", "decision": "Three.js + Vite, plain JS modules"}],
    "assumptions": ["Node 20 is available"],
    "epics": [{"title": "Site", "tasks": [
        {"key": "scaffold", "title": "scaffold scene", "description": "scene, camera, lights", "agent": "frontend", "files": ["app/**"], "acceptance": "scene renders", "verify_cmd": "true"},
        {"key": "luffy", "title": "luffy model", "description": "luffy", "agent": "frontend", "files": ["models/luffy/**"], "deps": ["scaffold"], "acceptance": "model", "verify_cmd": "true"},
        {"key": "zoro", "title": "zoro model", "description": "zoro", "agent": "frontend", "files": ["models/zoro/**"], "deps": ["scaffold"], "acceptance": "model", "verify_cmd": "true"},
        {"key": "ui", "title": "ui labels", "description": "ui", "agent": "frontend", "files": ["ui/**"], "deps": ["luffy", "zoro"], "acceptance": "labels", "verify_cmd": "true"}]}],
}
OWNED = [{"owned_file": True}, {"commit": True}, {"submit": True}]


class CondCase(WorkerCase):
    def setUp(self):
        super().setUp()
        os.environ["MESH_ROOT"] = ""

    def project(self, plan=PLAN, scenario=None, **kw):
        r = self.proj(**kw)
        os.environ["MESH_ROOT"] = str(r.root)
        os.environ["FAKE_PLAN"] = json.dumps(plan)
        self.scenario(scenario if scenario is not None else OWNED)
        self.paths = Paths(r.root)
        self.cond = conductor.Conductor(self.paths)
        self.addCleanup(self._close)
        return r

    def _close(self):
        try:
            db = DB(self.paths)
            for w in workers.list_workers(db, active_only=True):
                workers.terminate(db, w["id"], "test")
            db.close()
        except Exception:
            pass
        try:
            self.cond.close()
        except Exception:
            pass

    def cmd(self, kind, **payload):
        cid = runs.enqueue(self.cond.db, kind, payload, "human")
        return cid

    def run_until(self, cond_fn, timeout=90, step=0.25):
        end = time.time() + timeout
        while time.time() < end:
            self.cond.tick()
            if cond_fn():
                return True
            time.sleep(step)
        self.cond.tick()
        return cond_fn()

    def cur_run(self):
        return runs.latest_run(self.cond.db)

    def status(self):
        return (self.cur_run() or {"status": None})["status"]

    def calls(self, kind):
        p = self.state / f"calls.{kind}"
        return int(p.read_text()) if p.exists() else 0


class TestFullRun(CondCase):
    def test_prompt_to_complete_with_no_llm_driving_execution(self):
        r = self.project(); db = self.cond.db
        self.cmd("submit_prompt", prompt="Build a One Piece 3D site", cli="claude")
        self.assertTrue(self.run_until(lambda: self.status() == "awaiting_approval"), self.status())
        self.assertEqual(self.calls("plan"), 1)
        # plan became real tasks + ADRs, and nothing is dispatched before the user approves
        self.assertEqual(db.one("SELECT COUNT(*) c FROM tasks WHERE kind='task' AND run_id=?", (self.cur_run()["id"],))["c"], 4)
        self.assertTrue(list((r.root / ".mesh/memory/decisions").glob("ADR-*")))
        for _ in range(6):
            self.cond.tick(); time.sleep(0.1)
        self.assertEqual(db.one("SELECT COUNT(*) c FROM workers")["c"], 0)
        att = runs.open_attention(db)
        self.assertEqual([a["kind"] for a in att], ["plan_approval"])
        self.cmd("approve_item", id=att[0]["id"])
        self.assertTrue(self.run_until(lambda: self.status() in ("complete", "failed"), 150), (self.status(), [dict(t) for t in db.q("select title,status from tasks")]))
        self.assertEqual(self.status(), "complete", self.cur_run()["error"])
        # every leaf task completed, merged, verified by CODE; the only LLM calls were the plan (no brain turn was ever needed)
        self.assertEqual({t["status"] for t in db.q("SELECT status FROM tasks WHERE kind='task'")}, {"COMPLETED"})
        self.assertEqual(self.calls("brain"), 0)
        iw = db.paths.worktrees / "_integration"
        for d in ("app", "models/luffy", "models/zoro", "ui"):
            self.assertTrue(list((iw / d).glob("*.txt")), d)
        self.assertEqual(db.one("SELECT COUNT(*) c FROM events WHERE type='integrate.ok'")["c"], 4)
        # dependency order respected by the conductor
        order = [json.loads(e["data"])["worker"] for e in db.q("SELECT data FROM events WHERE type='worker.spawn' ORDER BY id")]
        titles = [db.one("SELECT t.title FROM workers w JOIN tasks t ON t.id=w.task_id WHERE w.id=?", (w,))["title"] for w in order]
        self.assertEqual(titles[0], "scaffold scene"); self.assertEqual(titles[-1], "ui labels")
        # the run ends by asking the human to promote; promotion is never automatic
        self.assertEqual([a["kind"] for a in runs.open_attention(db)], ["promote"])
        self.assertIn("PASS", self.cur_run()["final_validation"])
        # exactly one orchestrator session, labelled as such; workers never are
        roles = [s["role"] for s in db.q("SELECT role FROM sessions WHERE role='orchestrator'")]
        self.assertEqual(roles, ["orchestrator"])
        self.assertTrue(usage.latest_series(db))  # usage recorded for planner (brain-N) and workers

    def test_REGRESSION_tasks_created_but_never_executed(self):
        """The failure seen in a real project: tasks existed (one READY) and nothing ever spawned a worker."""
        r = self.project(); db = self.cond.db
        rid = runs.create_run(db, "manual objective", "claude")
        runs.set_status(db, rid, "executing", "test")
        core.register_session(db, "claude", "orchestrator", None, os.getpid(), None, role="orchestrator", session_id=f"orch-{rid}")
        epic = tasks.create(db, "E", kind="epic", run_id=rid)
        a = tasks.create(db, "a", parent=epic, files=["a/**"], agent="frontend", verify_cmd="true", run_id=rid)
        b = tasks.create(db, "b", parent=epic, files=["b/**"], agent="frontend", deps=[a], verify_cmd="true", run_id=rid)
        self.assertEqual(tasks.get(db, a)["status"], "READY"); self.assertEqual(tasks.get(db, b)["status"], "BACKLOG")
        self.assertTrue(self.run_until(lambda: tasks.get(db, b)["status"] == "COMPLETED", 90))
        self.assertEqual(tasks.get(db, a)["status"], "COMPLETED")
        self.assertEqual(self.calls("plan") + self.calls("brain"), 0)  # zero LLM involvement in execution

    def test_plan_approval_can_be_disabled(self):
        r = self.project(); db = self.cond.db
        from mesh.core import set_toml_section
        a = core.load_config(self.paths)["autonomy"]; a["plan_approval"] = False
        set_toml_section(self.paths.config, "autonomy", a)
        self.cmd("submit_prompt", prompt="go", cli="claude")
        self.assertTrue(self.run_until(lambda: self.status() == "executing"))
        self.assertEqual(runs.open_attention(db), [])

    def test_only_one_active_run_and_messages_become_directives(self):
        r = self.project(); db = self.cond.db
        self.cmd("submit_prompt", prompt="first", cli="claude")
        self.assertTrue(self.run_until(lambda: self.status() == "awaiting_approval"))
        c2 = self.cmd("submit_prompt", prompt="also make it blue")
        self.cond.tick()
        self.assertEqual(db.one("SELECT result FROM commands WHERE id=?", (c2,))["result"], "sent to the orchestrator")
        self.assertEqual(db.one("SELECT COUNT(*) c FROM runs")["c"], 1)
        self.assertTrue(db.q("SELECT 1 FROM messages WHERE recipient='role:orchestrator' AND sender='human'"))

    def test_halt_command_stops_workers_and_run(self):
        r = self.project(scenario=[{"sleep": 60}]); db = self.cond.db
        self.cmd("submit_prompt", prompt="x", cli="claude")
        self.run_until(lambda: self.status() == "awaiting_approval")
        self.cmd("approve_item", id=runs.open_attention(db)[0]["id"])
        self.assertTrue(self.run_until(lambda: workers.active_count(db) >= 1))
        self.cmd("halt")
        self.assertTrue(self.run_until(lambda: self.status() == "halted"))
        self.assertEqual(workers.active_count(db), 0)
        self.cmd("resume"); self.assertTrue(self.run_until(lambda: self.status() == "executing"))


class TestPlanner(CondCase):
    def test_invalid_plan_is_repaired_once(self):
        bad = json.loads(json.dumps(PLAN))
        bad["epics"][0]["tasks"][2]["files"] = ["models/luffy/x.js"]  # zoro now overlaps luffy, and the two are unordered -> invalid
        r = self.project()
        os.environ["FAKE_PLAN_SEQ"] = json.dumps([bad, PLAN])
        self.cmd("submit_prompt", prompt="x", cli="claude")
        self.assertTrue(self.run_until(lambda: self.status() == "awaiting_approval"))
        self.assertEqual(self.calls("plan"), 2)  # exactly one automatic repair retry
        self.assertEqual(self.cond.db.one("SELECT COUNT(*) c FROM tasks WHERE kind='task'")["c"], 4)

    def test_unrecoverable_planner_failure_is_visible_and_replannable(self):
        r = self.project(); db = self.cond.db
        os.environ["FAKE_PLANNER_FAIL"] = "1"
        self.cmd("submit_prompt", prompt="x", cli="claude")
        self.assertTrue(self.run_until(lambda: any(a["kind"] == "error" for a in runs.open_attention(db))))
        self.assertEqual(self.status(), "planning")
        del os.environ["FAKE_PLANNER_FAIL"]
        self.cmd("replan")
        self.assertTrue(self.run_until(lambda: self.status() == "awaiting_approval"))

    def test_plan_json_in_text_and_per_cli_formats(self):
        for cli, struct in (("claude", "0"), ("antigravity", "1"), ("antigravity", "0"), ("copilot", "1")):
            r = self.project()  # fresh project each time
            os.environ["FAKE_STRUCT"] = struct
            self.cmd("submit_prompt", prompt="x", cli=cli)
            self.assertTrue(self.run_until(lambda: self.status() == "awaiting_approval"), (cli, struct, self.status()))
            self.cond.close()

    def test_validate_plan_rules(self):
        from mesh import brain
        ids = {"frontend", "backend", "qa"}
        ok = lambda p: brain.validate_plan(p, ids)
        self.assertEqual(ok(PLAN), [])
        p = json.loads(json.dumps(PLAN)); p["epics"][0]["tasks"][0]["agent"] = "wizard"; self.assertIn("unknown agent", " ".join(ok(p)))
        p = json.loads(json.dumps(PLAN)); p["epics"][0]["tasks"][0]["deps"] = ["ui"]; self.assertIn("cycle", " ".join(ok(p)))
        p = json.loads(json.dumps(PLAN)); p["epics"][0]["tasks"][1]["deps"] = ["ghost"]; self.assertIn("unknown key", " ".join(ok(p)))
        p = json.loads(json.dumps(PLAN)); p["epics"][0]["tasks"][1]["files"] = []; self.assertIn("files", " ".join(ok(p)))
        self.assertEqual(ok({"epics": []}), ["plan has no epics"])
        self.assertIn("not a JSON object", ok("nope")[0])
        from mesh.brain import extract_json
        self.assertEqual(extract_json('chatter ```json\n{"a": {"b": "}"}}\n``` more'), {"a": {"b": "}"}})
        self.assertIsNone(extract_json("no json here"))


class TestJudgement(CondCase):
    def _approved_run(self, scenario):
        r = self.project(scenario=scenario); db = self.cond.db
        self.cmd("submit_prompt", prompt="x", cli="claude")
        self.run_until(lambda: self.status() == "awaiting_approval")
        self.cmd("approve_item", id=runs.open_attention(db)[0]["id"])
        return r

    def test_worker_question_wakes_brain_and_gets_an_answer(self):
        r = self._approved_run([{"when_title": "scaffold", "sh": "{mesh} msg send --to role:orchestrator --type question --requires-ack \"which database?\" --task \"$MESH_TASK\""},
                                {"owned_file": True}, {"commit": True}, {"submit": True}])
        os.environ["FAKE_BRAIN"] = json.dumps({"actions": [{"type": "reply", "to": "$MSG", "text": "use SQLite"}]})
        db = self.cond.db
        self.assertTrue(self.run_until(lambda: self.calls("brain") >= 1, 90))
        self.assertTrue(self.run_until(lambda: db.q("SELECT 1 FROM messages WHERE type='answer' AND content='use SQLite'"), 30))
        q = db.one("SELECT acked FROM messages WHERE type='question'")
        self.assertEqual(q["acked"], 1)  # handled exactly once
        self.cond.tick(); time.sleep(0.5); self.cond.tick()
        self.assertEqual(self.calls("brain"), 1)  # not re-triggered
        self.assertEqual(db.one("SELECT brain_session FROM runs")["brain_session"], "fake-brain-session")

    def test_invalid_brain_actions_are_skipped_and_surfaced(self):
        r = self._approved_run([{"when_title": "scaffold", "sh": "{mesh} msg send --to role:orchestrator --type blocker --requires-ack \"stuck\" --task \"$MESH_TASK\""},
                                {"owned_file": True}, {"commit": True}, {"submit": True}])
        os.environ["FAKE_BRAIN"] = json.dumps({"actions": [{"type": "rm_rf", "path": "/"}, {"type": "reply"}]})
        db = self.cond.db
        self.assertTrue(self.run_until(lambda: any("invalid" in a["title"] for a in runs.open_attention(db)), 90))
        self.assertEqual(db.one("SELECT COUNT(*) c FROM messages WHERE type='answer'")["c"], 0)  # nothing executed

    def test_verification_failure_is_reworked_then_escalated(self):
        plan = json.loads(json.dumps(PLAN)); plan["epics"][0]["tasks"] = [
            {"key": "x", "title": "needs file", "description": "d", "agent": "frontend", "files": ["x/**"], "acceptance": "a", "verify_cmd": "test -f x/never.txt"}]
        r = self.project(plan=plan, scenario=OWNED); db = self.cond.db
        self.cmd("submit_prompt", prompt="x", cli="claude")
        self.run_until(lambda: self.status() == "awaiting_approval")
        self.cmd("approve_item", id=runs.open_attention(db)[0]["id"])
        self.assertTrue(self.run_until(lambda: any(a["kind"] == "escalation" for a in runs.open_attention(db)), 150),
                        [dict(a) for a in runs.open_attention(db)])
        self.assertEqual(db.one("SELECT COUNT(*) c FROM events WHERE type='task.rejected'")["c"], 2)  # max_rework automatic retries
        self.assertEqual(self.status(), "executing")  # never silently marked complete

    def test_repeated_worker_failure_blocks_task_and_escalates(self):
        r = self.project(scenario=[{"exit": 1}]); db = self.cond.db
        from mesh.core import set_toml_section
        set_toml_section(self.paths.config, "workers", {"max_restarts": 0})
        self.cmd("submit_prompt", prompt="x", cli="claude")
        self.run_until(lambda: self.status() == "awaiting_approval")
        self.cmd("approve_item", id=runs.open_attention(db)[0]["id"])
        self.assertTrue(self.run_until(lambda: any(a["kind"] == "escalation" for a in runs.open_attention(db)), 150))
        t = db.one("SELECT status FROM tasks WHERE title='scaffold scene'")
        self.assertEqual(t["status"], "BLOCKED")
        self.assertEqual(db.one("SELECT COUNT(*) c FROM workers WHERE state='FAILED'")["c"], 3)  # max_task_attempts

    def test_dispatch_failure_is_visible_not_silent(self):
        r = self.project(); db = self.cond.db
        self.cmd("submit_prompt", prompt="x", cli="claude")
        self.run_until(lambda: self.status() == "awaiting_approval")
        self.cmd("approve_item", id=runs.open_attention(db)[0]["id"])
        os.environ.update({"MESH_BIN_CLAUDE": "/nonexistent", "MESH_BIN_COPILOT": "/nonexistent", "MESH_BIN_ANTIGRAVITY": "/nonexistent"})
        self.assertTrue(self.run_until(lambda: db.q("SELECT 1 FROM events WHERE type='dispatch.error'"), 20))
        self.assertTrue(any(a["kind"] == "error" and "Could not start" in a["title"] for a in runs.open_attention(db)))

    def test_weak_verification_asks_the_user_who_approves_from_the_queue(self):
        plan = json.loads(json.dumps(PLAN)); plan["validation_cmd"] = ""
        plan["epics"][0]["tasks"] = [{"key": "w", "title": "weak one", "description": "d", "agent": "frontend", "files": ["w/**"], "acceptance": "a", "verify_cmd": ""}]
        r = self.project(plan=plan); db = self.cond.db
        from mesh.core import set_toml_section
        set_toml_section(self.paths.config, "validation", {"test": "", "lint": ""})
        self.cmd("submit_prompt", prompt="x", cli="claude"); self.run_until(lambda: self.status() == "awaiting_approval")
        self.cmd("approve_item", id=runs.open_attention(db)[0]["id"])
        self.assertTrue(self.run_until(lambda: any(a["kind"] == "task_review" for a in runs.open_attention(db)), 90))
        self.assertEqual(db.one("SELECT status FROM tasks WHERE title='weak one'")["status"], "REVIEW")  # not auto-approved
        a = next(a for a in runs.open_attention(db) if a["kind"] == "task_review")
        self.cmd("approve_item", id=a["id"])
        self.assertTrue(self.run_until(lambda: db.one("SELECT status FROM tasks WHERE title='weak one'")["status"] == "COMPLETED", 30))
        self.assertTrue(self.run_until(lambda: self.status() == "complete", 60))


class TestLifecycle(CondCase):
    def test_single_instance_lock_and_status(self):
        r = self.repo(); paths = Paths(r.root)
        lock = conductor.acquire_lock(paths)
        self.assertIsNotNone(lock)
        self.assertIsNone(conductor.acquire_lock(paths))  # second conductor impossible
        lock.close()
        self.assertIsNotNone(conductor.acquire_lock(paths))

    def test_daemon_start_status_stop_and_kill9_recovery(self):
        r = self.proj(); os.environ["MESH_ROOT"] = str(r.root); paths = Paths(r.root)
        pid = conductor.start_daemon(paths)
        self.assertTrue(wait_for(lambda: conductor.status(paths)["running"], 15))
        self.assertEqual(conductor.start_daemon(paths), conductor.status(paths)["pid"])  # idempotent
        os.kill(pid, signal.SIGKILL)  # crash
        self.assertTrue(wait_for(lambda: not workers.pid_alive(pid), 10))
        self.assertFalse(conductor.status(paths)["running"])
        pid2 = conductor.start_daemon(paths)  # lock was released by the OS; a new one takes over
        self.assertNotEqual(pid2, pid)
        self.assertTrue(wait_for(lambda: conductor.status(paths)["running"], 15))
        self.assertTrue(conductor.stop_daemon(paths))
        self.assertTrue(wait_for(lambda: not conductor.status(paths)["running"], 15))

    def test_cli_start_wait_end_to_end_via_daemon(self):
        r = self.proj(); os.environ["MESH_ROOT"] = str(r.root); os.environ["FAKE_PLAN"] = json.dumps(PLAN); self.scenario(OWNED)
        out = r.mesh("start", "Build it", "--cli", "claude", "--wait")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("awaiting_approval", out.stdout); self.assertIn("needs you: [plan_approval]", out.stdout)
        st = r.mesh("status"); self.assertEqual(st.returncode, 0); self.assertIn("conductor: running", st.stdout); self.assertIn("NEEDS YOU", st.stdout)
        db = r.db(); aid = runs.open_attention(db)[0]["id"]
        self.assertEqual(r.mesh("ctl", "approve_item", json.dumps({"id": aid})).returncode, 0)
        paths = Paths(r.root)
        self.assertTrue(wait_for(lambda: (runs.latest_run(db) or {"status": ""})["status"] == "complete", 150))
        conductor.stop_daemon(paths)

    def test_stale_sessions_age_out_and_orchestrator_is_singular(self):
        r = self.repo(); db = r.db()
        s = r.session("antigravity", "ghost0001")
        db.conn.execute("UPDATE sessions SET last_seen=? WHERE id=?", (time.time() - 5000, s))
        w = core.register_session(db, "claude", "worker", None, os.getpid(), None, role="worker", session_id="w-keep")
        db.conn.execute("UPDATE sessions SET last_seen=? WHERE id=?", (time.time() - 5000, w))
        self.assertEqual(core.mark_stale(db, 1800), 1)  # worker liveness belongs to the runner, so it is excluded
        self.assertEqual(db.one("SELECT status FROM sessions WHERE id=?", (s,))["status"], "stale")
        self.assertEqual(db.one("SELECT status FROM sessions WHERE id=?", ("w-keep",))["status"], "active")
        for i in range(3):
            core.register_session(db, "copilot", "orchestrator", None, os.getpid(), None, role="orchestrator", session_id=f"orch-{i}")
        self.assertEqual(db.one("SELECT COUNT(*) c FROM sessions WHERE role='orchestrator' AND status='active'")["c"], 1)
        from mesh import health
        self.assertFalse(any(l == "bad" and "orchestrator" in t for l, t in health.checks(db)))

    def test_health_flags_version_skew_and_uncommitted_files(self):
        r = self.repo(); db = r.db()
        (r.root / ".mesh" / "lib" / "mesh" / "__init__.py").write_text('__version__ = "0.0.1"\n')
        from mesh import health
        msgs = " | ".join(t for _, t in health.checks(db))
        self.assertIn("version skew", msgs); self.assertIn("uncommitted", msgs)


if __name__ == "__main__":
    unittest.main()
