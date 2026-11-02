"""Worker process model with a fake CLI: spawn, parallelism, crash/kill-9 recovery, limit fallback, guards, usage."""
import json
import os
import signal
import sys
import time
import unittest
from pathlib import Path

from helpers import MeshCase, Repo, SRC, sh
from mesh import core, tasks, usage, workers
from mesh.db import MeshError

FAKE = str(Path(__file__).resolve().parent / "fakecli" / "fake.py")


def wait_for(fn, timeout=30, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return fn()


class WorkerCase(MeshCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.state = Path(os.environ.get("TMPDIR", "/tmp")) / f"fake-{os.getpid()}-{id(self)}"
        self.state.mkdir(parents=True, exist_ok=True)
        os.environ.update({"MESH_BIN_CLAUDE": FAKE, "MESH_BIN_COPILOT": FAKE, "MESH_BIN_ANTIGRAVITY": FAKE,
                           "FAKE_STATE_DIR": str(self.state), "FAKE_MESH_CMD": f"{sys.executable} -m mesh",
                           "PYTHONPATH": str(SRC)})
        os.environ.pop("MESH_SESSION", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear(); os.environ.update(self._env)

    def scenario(self, s):
        os.environ["FAKE_SCENARIO"] = json.dumps(s)

    def proj(self, **kw):
        r = self.repo(**kw)
        os.environ["MESH_ROOT"] = str(r.root)
        r.mesh("autonomy", "accept", "--yes")
        # committed config so worktrees carry hooks/instructions (spawn refuses otherwise)
        r.mesh("sync"); r.mesh("commit-config")
        return r

    def kill_all(self, r):
        db = r.db()
        for w in workers.list_workers(db, active_only=True):
            workers.terminate(db, w["id"], "test")

    def done(self, r, wid, states=("COMPLETED", "FAILED", "TERMINATED"), timeout=40):
        db = r.db()
        return wait_for(lambda: workers.get(db, wid)["state"] in states, timeout)


class TestSpawn(WorkerCase):
    def test_spawn_requires_autonomy_and_committed_config(self):
        r = self.repo(); os.environ["MESH_ROOT"] = str(r.root); db = r.db()
        t = tasks.create(db, "t", files=["a/**"])
        with self.assertRaises(MeshError) as cm:
            workers.spawn(db, t)
        self.assertIn("autonomy", str(cm.exception))
        r.mesh("autonomy", "accept", "--yes")
        with self.assertRaises(MeshError) as cm:
            workers.spawn(db, t)
        self.assertIn("not committed", str(cm.exception))
        self.assertEqual(tasks.get(db, t)["status"], "READY")  # failed spawn left no ownership behind

    def test_worker_runs_submits_and_cannot_complete(self):
        r = self.proj(); db = r.db()
        t = tasks.create(db, "feature", files=["out.txt"], acceptance="out.txt exists")
        w = workers.spawn(db, t, agent="backend", cli="claude")
        self.assertTrue(self.done(r, w["id"]), "worker did not finish")
        wr = workers.get(db, w["id"])
        self.assertEqual(wr["state"], "COMPLETED", dict(wr))
        task = tasks.get(db, t)
        self.assertEqual(task["status"], "REVIEW")  # submitted, NOT completed
        self.assertTrue((Path(wr["worktree"]) / "out.txt").exists())
        self.assertEqual(wr["native_session"], f"fake-claude-{w['id']}")
        u = usage.latest(db, w["id"])
        self.assertEqual((u["source"], u["input_tokens"] > 0, u["cost_usd"]), ("reported", True, 0.01))
        self.assertEqual(u["context_size"], 200000)
        # the worker session cannot complete its own task
        res = r.mesh("task", "complete", t, "--as", w["id"])
        self.assertNotEqual(res.returncode, 0); self.assertIn("worker session", res.stderr)

    def test_five_parallel_workers_and_caps(self):
        r = self.proj(); db = r.db()
        self.scenario([{"sleep": 1.5}, {"write": "f.txt", "content": "x"}, {"commit": True}, {"submit": True}])
        ids = [tasks.create(db, f"t{i}", files=[f"m{i}/**"]) for i in range(5)]
        ws = [workers.spawn(db, t, cli="claude") for t in ids]
        self.assertEqual(len({w["id"] for w in ws}), 5)
        self.assertEqual(workers.active_count(db), 5)
        for w in ws:
            self.assertTrue(self.done(r, w["id"]))
        self.assertTrue(all(tasks.get(db, t)["status"] == "REVIEW" for t in ids))
        # cap
        from mesh.core import set_toml_section
        set_toml_section(db.paths.config, "workers", {"max_workers": 1})
        a, b = tasks.create(db, "a", files=["a1/**"]), tasks.create(db, "b", files=["b1/**"])
        self.scenario([{"sleep": 20}])
        w1 = workers.spawn(db, a, cli="claude")
        with self.assertRaises(MeshError) as cm:
            workers.spawn(db, b, cli="claude")
        self.assertIn("cap", str(cm.exception))
        self.assertEqual(tasks.get(db, b)["status"], "READY")
        self.kill_all(r)

    def test_duplicate_spawn_for_same_task_prevented(self):
        r = self.proj(); db = r.db()
        self.scenario([{"sleep": 20}])
        t = tasks.create(db, "t", files=["x/**"])
        w = workers.spawn(db, t, cli="claude")
        with self.assertRaises(MeshError):
            workers.spawn(db, t, cli="claude")
        self.assertEqual(db.one("SELECT COUNT(*) c FROM workers WHERE task_id=?", (t,))["c"], 1)
        self.kill_all(r)


class TestRecovery(WorkerCase):
    def test_kill9_runner_group_detected_and_task_resumed(self):
        r = self.proj(); db = r.db()
        self.scenario({"attempts": [[{"sleep": 60}], [{"write": "r.txt", "content": "after restart"}, {"commit": True}, {"submit": True}]]})
        t = tasks.create(db, "victim", files=["r.txt"])
        w = workers.spawn(db, t, cli="claude")
        wait_for(lambda: workers.get(db, w["id"])["pid"])
        wr = workers.get(db, w["id"])
        os.killpg(wr["pgid"], signal.SIGKILL)  # whole group: runner + CLI vanish, like a reboot/kill -9
        wait_for(lambda: not workers.pid_alive(wr["runner_pid"], wr["runner_start"]), 10)
        acted = workers.reconcile(db)
        self.assertTrue(any("restart" in x for x in acted), acted)
        self.assertTrue(self.done(r, w["id"]))
        wr = workers.get(db, w["id"])
        self.assertEqual((wr["state"], wr["restarts"]), ("COMPLETED", 1), dict(wr))
        self.assertEqual(tasks.get(db, t)["status"], "REVIEW")
        self.assertTrue(db.q("SELECT 1 FROM checkpoints WHERE reason='worker-crash'"))

    def test_usage_limit_falls_back_to_other_cli(self):
        r = self.proj(); db = r.db()
        self.scenario({"by_cli": {"claude": [{"limit": True}], "copilot": [{"write": "c.txt", "content": "c"}, {"commit": True}, {"submit": True}]}})
        t = tasks.create(db, "limited", files=["c.txt"])
        w = workers.spawn(db, t, cli="claude")
        self.assertTrue(self.done(r, w["id"]))
        wr = workers.get(db, w["id"])
        self.assertEqual((wr["state"], wr["cli"], wr["restarts"]), ("COMPLETED", "copilot", 1), dict(wr))
        self.assertTrue(db.q("SELECT 1 FROM events WHERE type='worker.limit'"))
        self.assertTrue(db.q("SELECT 1 FROM events WHERE type='worker.fallback'"))
        self.assertTrue(db.q("SELECT 1 FROM ratelimits"))
        self.assertEqual(tasks.get(db, t)["status"], "REVIEW")
        # Copilot tokens come from the OTel export, labelled as such (reported tokens, no cost)
        u = usage.latest(db, w["id"])
        self.assertEqual((u["input_tokens"], u["output_tokens"]), (120, 30)); self.assertIsNone(u["cost_usd"])

    def test_limit_without_fallback_fails_and_releases_task(self):
        r = self.proj(); db = r.db()
        os.environ.update({"MESH_BIN_COPILOT": "/nonexistent", "MESH_BIN_ANTIGRAVITY": "/nonexistent"})
        self.scenario([{"limit": True}])
        t = tasks.create(db, "l", files=["l/**"])
        w = workers.spawn(db, t, cli="claude")
        self.assertTrue(self.done(r, w["id"]))
        self.assertEqual(workers.get(db, w["id"])["state"], "FAILED")
        self.assertEqual(tasks.get(db, t)["status"], "READY")  # released for the orchestrator to re-route
        self.assertTrue(db.q("SELECT 1 FROM messages WHERE recipient='role:orchestrator' AND type='blocker'"))

    def test_exit_without_submit_is_not_completion(self):
        r = self.proj(); db = r.db()
        self.scenario([{"say": "I think I'm done"}])
        t = tasks.create(db, "lazy", files=["z/**"])
        w = workers.spawn(db, t, cli="claude")
        self.assertTrue(self.done(r, w["id"]))
        self.assertEqual(workers.get(db, w["id"])["state"], "FAILED")
        self.assertEqual(tasks.get(db, t)["status"], "READY")
        self.assertTrue(db.q("SELECT 1 FROM events WHERE type='worker.exited_unsubmitted'"))

    def test_halt_kills_everything_and_blocks_spawns(self):
        r = self.proj(); db = r.db()
        self.scenario([{"sleep": 60}])
        ws = [workers.spawn(db, tasks.create(db, f"h{i}", files=[f"h{i}/**"]), cli="claude") for i in range(3)]
        wait_for(lambda: all(workers.get(db, w["id"])["pid"] for w in ws))
        res = r.mesh("halt")
        self.assertEqual(res.returncode, 0, res.stderr)
        for w in ws:
            wr = workers.get(db, w["id"])
            self.assertEqual(wr["state"], "TERMINATED")
            self.assertFalse(workers.pid_alive(wr["runner_pid"], wr["runner_start"]))
            self.assertFalse(workers.pid_alive(wr["pid"]))
        with self.assertRaises(MeshError):
            workers.spawn(db, tasks.create(db, "after", files=["q/**"]), cli="claude")

    def test_stall_is_detected_and_reported(self):
        r = self.proj(); db = r.db()
        from mesh.core import set_toml_section
        set_toml_section(db.paths.config, "workers", {"stall_seconds": 1})
        self.scenario([{"stall": True}])
        t = tasks.create(db, "s", files=["s/**"])
        w = workers.spawn(db, t, cli="claude")
        self.assertTrue(wait_for(lambda: db.q("SELECT 1 FROM events WHERE type='worker.stalled'"), 20))
        self.assertTrue(db.q("SELECT 1 FROM messages WHERE type='blocker' AND content LIKE '%no output%'"))
        self.kill_all(r)

    def test_worker_env_does_not_leak_ambient_secrets(self):
        r = self.proj(); db = r.db()
        os.environ["AWS_SECRET_ACCESS_KEY"] = "topsecret-should-not-leak"
        self.scenario([{"sh": "env > env.dump; git add -A; git -c user.email=f@f -c user.name=f commit -qm env; {mesh} task submit \"$MESH_TASK\""}])
        t = tasks.create(db, "e", files=["env.dump"])
        w = workers.spawn(db, t, cli="claude")
        self.assertTrue(self.done(r, w["id"]))
        dump = (Path(workers.get(db, w["id"])["worktree"]) / "env.dump").read_text()
        self.assertNotIn("topsecret", dump)
        self.assertIn("MESH_ROLE=worker", dump)


if __name__ == "__main__":
    unittest.main()
