"""App logic that does not need a terminal: command parsing/dispatch, activity feed wording, setup, packaging guarantees."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import MeshCase, Repo, sh
from test_workers import WorkerCase
from mesh import conductor, core, feed, runs, snapshot, tasks, workers
from mesh.app import commands, setup
from mesh.db import Paths


class TestCommands(MeshCase):
    def snap(self, r):
        return snapshot.build(Paths(r.root))

    def test_parse(self):
        self.assertEqual(commands.parse("Build me a site"), ("/new", "Build me a site"))
        self.assertEqual(commands.parse("/Approve"), ("/approve", ""))
        self.assertEqual(commands.parse("/say w-1 hello there"), ("/say", "w-1 hello there"))
        self.assertEqual(commands.parse("   "), (None, ""))

    def test_commands_enqueue_for_the_conductor_and_never_mutate_state(self):
        r = self.repo(); paths = Paths(r.root); db = r.db()
        with mock.patch.object(conductor, "start_daemon", return_value=1) as sd:
            rep = commands.execute(paths, self.snap(r), "Build a 3D bottle site")
            self.assertEqual(rep.kind, "message"); self.assertIn("Starting", rep.text)
            c = db.one("SELECT * FROM commands WHERE kind='submit_prompt'")
            self.assertEqual(json.loads(c["payload"])["prompt"], "Build a 3D bottle site")
            self.assertEqual(c["status"], "pending")  # only the conductor may act on it
            self.assertTrue(sd.called)
            self.assertEqual(db.one("SELECT COUNT(*) c FROM runs")["c"], 0)  # the UI path created no run itself
            for text, kind in (("/pause", "pause"), ("/resume", "resume"), ("/halt", "halt"), ("/replan more tests", "replan"), ("/stop", "stop")):
                commands.execute(paths, self.snap(r), text)
                self.assertTrue(db.q("SELECT 1 FROM commands WHERE kind=?", (kind,)), text)

    def test_approve_reject_use_the_first_open_item(self):
        r = self.repo(); paths = Paths(r.root); db = r.db()
        with mock.patch.object(conductor, "start_daemon", return_value=1):
            self.assertIn("Nothing is waiting", commands.execute(paths, self.snap(r), "/approve").text)
            rid = runs.create_run(db, "x", "claude")
            aid = runs.add_attention(db, rid, "plan_approval", str(rid), "Plan ready", "detail")
            commands.execute(paths, self.snap(r), "/approve")
            c = db.one("SELECT * FROM commands WHERE kind='approve_item'")
            self.assertEqual(json.loads(c["payload"])["id"], aid)
            commands.execute(paths, self.snap(r), "/reject needs a database")
            self.assertEqual(json.loads(db.one("SELECT payload FROM commands WHERE kind='reject_item'")["payload"])["note"], "needs a database")
            self.assertIn("nothing to promote", commands.execute(paths, self.snap(r), "/promote").text.lower())

    def test_resolution_by_prefix_and_number_and_error_messages(self):
        r = self.repo(); paths = Paths(r.root); db = r.db()
        t1 = tasks.create(db, "alpha", files=["a/**"]); t2 = tasks.create(db, "beta", files=["b/**"])
        snap = self.snap(r)
        self.assertEqual(commands.resolve(snap, "#2", "task"), t2)
        self.assertEqual(commands.resolve(snap, t1[:6], "task"), t1)
        self.assertIsNone(commands.resolve(snap, "T-", "task"))  # ambiguous prefix is refused, not guessed
        self.assertIsNone(commands.resolve(snap, "#9", "task"))
        with mock.patch.object(conductor, "start_daemon", return_value=1):
            self.assertEqual(commands.execute(paths, snap, f"/task {t1}").kind, "screen")
            self.assertEqual(commands.execute(paths, snap, "/retry nope").level, "warn")
            self.assertEqual(commands.execute(paths, snap, "/cli bogus").level, "warn")
            self.assertEqual(commands.execute(paths, snap, "/wat").level, "warn")
            self.assertEqual(commands.execute(paths, snap, "/say onlyone").level, "warn")
            self.assertEqual(commands.execute(paths, snap, "/quit").kind, "quit")
            self.assertEqual(commands.execute(paths, snap, "/usage").screen, "usage")

    def test_every_documented_command_is_handled(self):
        r = self.repo(); paths = Paths(r.root); snap = self.snap(r)
        with mock.patch.object(conductor, "start_daemon", return_value=1):
            for name, args, _ in commands.COMMANDS:
                rep = commands.execute(paths, snap, name)
                self.assertNotIn("Unknown command", rep.text, name)


class TestFeed(MeshCase):
    def test_plain_language_and_titles_instead_of_ids(self):
        r = self.repo(); db = r.db(); paths = Paths(r.root)
        t = tasks.create(db, "Luffy model", files=["l/**"])
        db.event("conductor", "worker.spawn", t, {"worker": "w-1", "agent": "frontend", "cli": "claude"})
        db.event("conductor", "task.submitted", t, {})
        db.event("conductor", "verify.result", t, {"ok": True, "weak": False})
        db.event("conductor", "task.approved", t, {})
        db.event("conductor", "integrate.ok", t, {})
        db.event("w-1", "worker.limit", t, {"cli": "claude"})
        db.event("w-1", "worker.fallback", t, {"from": "claude", "to": "copilot"})
        db.event("mesh", "tool", t, {"tool": "Bash"})  # noise must not appear
        runs.enqueue(db, "submit_prompt", {"prompt": "Build it"}, "human")
        text = "\n".join(i["text"] for i in feed.activity(paths))
        for n in ("“Luffy model”", "Started frontend", "submitted for review", "verification passed", "approved", "merged into the integration branch",
                  "claude hit its usage limit", "Fell back from claude to copilot", "Build it"):
            self.assertIn(n, text)
        self.assertNotIn("Bash", text); self.assertNotIn(t, text)  # no raw ids
        keys = [i["key"] for i in feed.activity(paths)]
        self.assertEqual(len(keys), len(set(keys)))  # stable unique keys so the UI never double-prints


class TestSetup(MeshCase):
    def test_apply_setup_requires_acceptance_and_commits_config(self):
        r = self.repo(); paths = Paths(r.root)
        self.assertTrue(setup.needs_setup(paths))
        with mock.patch.object(setup, "installed_clis", return_value=["claude", "copilot"]):
            with self.assertRaises(Exception) as cm:
                setup.apply_setup(paths, "claude", {}, True, accepted=False)
            self.assertIn("accepted", str(cm.exception))
            with self.assertRaises(Exception):
                setup.apply_setup(paths, "antigravity", {}, True, accepted=True)  # not installed
            self.assertTrue(setup.needs_setup(paths))
            setup.apply_setup(paths, "claude", {"claude": "subscription", "copilot": "credits", "bogus": "x"}, False, accepted=True)
        cfg = core.load_config(paths)
        self.assertEqual((cfg["app"]["cli"], cfg["billing"]["claude"], cfg["billing"]["copilot"], cfg["autonomy"]["plan_approval"]), ("claude", "subscription", "credits", False))
        self.assertTrue(cfg["autonomy"]["accepted_at"])
        self.assertFalse(setup.needs_setup(paths))
        self.assertEqual(r.git("status", "--porcelain", "--", ".mesh", "AGENTS.md").stdout.strip(), "")  # config committed: workers inherit the hooks


class TestPackaging(MeshCase):
    def test_vendored_copy_excludes_the_app_and_hooks_never_import_textual(self):
        r = self.repo(init=False); r.mesh("init")
        self.assertFalse((r.root / ".mesh/lib/mesh/app").exists())  # hooks/workers use the stdlib-only core
        self.assertTrue((r.root / ".mesh/lib/mesh/conductor.py").exists())
        code = ("import sys; sys.path.insert(0, '.mesh/lib'); import mesh.hooks, mesh.cli, mesh.conductor, mesh.workers, mesh.runner, mesh.snapshot; "
                "print('textual' in sys.modules)")
        out = sh(sys.executable, "-c", code, cwd=r.root)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_no_args_outside_a_project_is_a_clear_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            env = {k: v for k, v in os.environ.items() if k != "MESH_ROOT"}
            env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src")
            res = sh(sys.executable, "-m", "mesh", cwd=d, env=env, input="")
            self.assertNotEqual(res.returncode, 0); self.assertIn("mesh init", res.stderr)


if __name__ == "__main__":
    unittest.main()


class TestNonInteractive(MeshCase):
    def test_app_entry_points_fail_fast_without_a_terminal(self):
        r = self.repo()
        for args in (["tui"], ["up", "build something"], []):
            res = sh(sys.executable, "-m", "mesh", *args, cwd=r.root, env=r.env(), input="")
            self.assertEqual(res.returncode, 2, (args, res.stderr)); self.assertIn("interactive terminal", res.stderr); self.assertNotIn("Traceback", res.stderr)
