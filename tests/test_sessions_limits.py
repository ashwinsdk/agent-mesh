"""Session visibility (live / idle / done shown; killed / cleared hidden), kill and clear, and the usage-limit collector."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import MeshCase
from test_workers import WorkerCase, wait_for
from mesh import conductor, core, feed, limits, runs, snapshot, tasks, workers
from mesh.app import commands
from mesh.db import DB, MeshError, Paths


def ids(snap):
    return {a["id"]: a for a in snap["agents"]}


class TestVisibility(MeshCase):
    def test_idle_and_done_sessions_are_listed_killed_and_cleared_are_not(self):
        r = self.repo(); db = r.db(); paths = Paths(r.root)
        live = r.session("claude", "live00001")
        idle = r.session("antigravity", "idle00001")
        db.conn.execute("UPDATE sessions SET last_seen=? WHERE id=?", (time.time() - 99999, idle))
        core.mark_stale(db, 1800)
        done = r.session("copilot", "done00001"); core.end_session(db, done)
        killed = r.session("claude", "kill00001"); clr = r.session("claude", "clr000001")
        core.end_session(db, clr)
        snap = snapshot.build(paths); a = ids(snap)
        self.assertEqual((a[live]["bucket"], a[idle]["bucket"], a[done]["bucket"]), ("live", "idle", "done"))
        self.assertEqual(a[idle]["state"], "IDLE"); self.assertEqual(a[done]["state"], "COMPLETED")
        core.kill_session(db, killed)
        core.clear_sessions(db, clr)
        a = ids(snapshot.build(paths))
        self.assertNotIn(killed, a); self.assertNotIn(clr, a)  # hidden...
        self.assertIn(done, a); self.assertIn(idle, a); self.assertIn(live, a)
        row = db.one("SELECT hidden, status FROM sessions WHERE id=?", (killed,))
        self.assertEqual((row["hidden"], row["status"]), ("killed", "killed"))  # ...but kept in history
        self.assertEqual(db.one("SELECT hidden FROM sessions WHERE id=?", (clr,))["hidden"], "cleared")

    def test_bulk_clear_scopes_and_live_sessions_are_protected(self):
        r = self.repo(); db = r.db(); paths = Paths(r.root)
        live = r.session("claude", "live00001")
        idle = r.session("antigravity", "idle00001"); db.conn.execute("UPDATE sessions SET last_seen=? WHERE id=?", (time.time() - 99999, idle)); core.mark_stale(db, 1800)
        done = r.session("copilot", "done00001"); core.end_session(db, done)
        with self.assertRaises(MeshError) as cm:
            core.clear_sessions(db, live)
        self.assertIn("kill it instead", str(cm.exception))
        self.assertEqual(core.clear_sessions(db, "done"), 1)
        a = ids(snapshot.build(paths)); self.assertNotIn(done, a); self.assertIn(idle, a); self.assertIn(live, a)
        self.assertEqual(core.clear_sessions(db, "idle"), 1)
        a = ids(snapshot.build(paths)); self.assertNotIn(idle, a); self.assertIn(live, a)  # live is never bulk-cleared
        with self.assertRaises(MeshError):
            core.clear_sessions(db, "nope")

    def test_orchestrator_cannot_be_killed_and_signals_only_matching_processes(self):
        r = self.repo(); db = r.db()
        core.register_session(db, "claude", "orchestrator", None, os.getpid(), None, role="orchestrator", session_id="orch-1")
        with self.assertRaises(MeshError) as cm:
            core.kill_session(db, "orch-1")
        self.assertIn("belongs to the run", str(cm.exception))
        victim = subprocess.Popen(["sleep", "30"])
        try:
            self.assertFalse(core._signal_cli_process(victim.pid, "claude"))  # not a claude process: left alone
            self.assertIsNone(victim.poll())
            self.assertFalse(core._signal_cli_process(os.getpid(), "claude"))  # never signals itself
        finally:
            victim.kill(); victim.wait()


class TestKillWorkers(WorkerCase):
    def proj_conductor(self):
        r = self.proj(); os.environ["MESH_ROOT"] = str(r.root)
        self.paths = Paths(r.root)
        self.cond = conductor.Conductor(self.paths)
        self.addCleanup(self.cond.close)
        return r

    def test_killing_a_live_worker_stops_it_hides_it_and_parks_its_task(self):
        r = self.proj_conductor(); db = self.cond.db
        self.scenario([{"sleep": 60}])
        t = tasks.create(db, "long job", files=["x/**"])
        w = workers.spawn(db, t, cli="claude")
        wait_for(lambda: workers.get(db, w["id"])["pid"])
        runs.enqueue(db, "kill_session", {"id": w["id"]}, "human")
        self.cond.tick()
        wr = workers.get(db, w["id"])
        self.assertEqual((wr["state"], wr["hidden"]), ("TERMINATED", "killed"))
        self.assertFalse(workers.pid_alive(wr["runner_pid"], wr["runner_start"]))
        self.assertEqual(tasks.get(db, t)["status"], "BLOCKED")  # parked, NOT silently re-dispatched
        self.assertNotIn(w["id"], ids(snapshot.build(self.paths)))
        res = feed.command_results(self.paths, 0)
        self.assertTrue(any("killed" in (x["text"] or "") and "/retry" in (x["text"] or "") for x in res), res)

    def test_done_worker_is_listed_then_cleared_and_live_worker_cannot_be_cleared(self):
        r = self.proj_conductor(); db = self.cond.db
        self.scenario([{"owned_file": True}, {"commit": True}, {"submit": True}])
        t = tasks.create(db, "quick", files=["q/**"])
        w = workers.spawn(db, t, cli="claude")
        self.assertTrue(wait_for(lambda: workers.get(db, w["id"])["state"] == "COMPLETED", 40))
        a = ids(snapshot.build(self.paths))
        self.assertEqual(a[w["id"]]["bucket"], "done")
        self.scenario([{"sleep": 60}])
        t2 = tasks.create(db, "live one", files=["l/**"]); w2 = workers.spawn(db, t2, cli="claude")
        wait_for(lambda: workers.get(db, w2["id"])["pid"])
        runs.enqueue(db, "clear_session", {"scope": w2["id"]}, "human"); self.cond.tick()
        err = db.one("SELECT status, result FROM commands WHERE kind='clear_session' ORDER BY id DESC LIMIT 1")
        self.assertEqual(err["status"], "error"); self.assertIn("kill it instead", err["result"])
        runs.enqueue(db, "clear_session", {"scope": "done"}, "human"); self.cond.tick()
        a = ids(snapshot.build(self.paths))
        self.assertNotIn(w["id"], a); self.assertIn(w2["id"], a)
        workers.terminate(db, w2["id"], "test")

    def test_worker_ended_by_approval_counts_as_done_not_killed(self):
        r = self.proj_conductor(); db = self.cond.db
        t = tasks.create(db, "approved", files=["a/**"], verify_cmd="true")
        w = workers.spawn(db, t, cli="claude", start=False)
        tasks.transition(db, t, "IN_PROGRESS", w["id"])
        wt = __import__("pathlib").Path(w["worktree"]); (wt / "a").mkdir(); (wt / "a" / "f.txt").write_text("x")
        r.git("add", "-A", cwd=wt); r.git("commit", "-qm", "w", cwd=wt)
        from mesh import verify
        verify.submit(db, t, w["id"], "done"); verify.verify(db, t, "orch"); verify.approve(db, t, "orch")
        a = ids(snapshot.build(self.paths))[w["id"]]
        self.assertEqual((a["state"], a["bucket"]), ("COMPLETED", "done"))


class TestLimitParsing(unittest.TestCase):
    def w(self, raw):
        return {x["window"]: x for x in limits.parse(raw)}

    def test_json_shapes(self):
        j = json.dumps({"quotas": [{"name": "Daily", "usedPercent": 42, "resetsAt": "2026-11-03T09:00:00Z"},
                                   {"window": "weekly", "remainingFraction": 0.9, "resetTime": 1793900000}]})
        w = self.w(j)
        self.assertAlmostEqual(w["daily"]["used_pct"], 42); self.assertAlmostEqual(w["daily"]["remaining_pct"], 58)
        self.assertTrue(w["daily"]["resets_ts"])
        self.assertAlmostEqual(w["weekly"]["remaining_pct"], 90); self.assertAlmostEqual(w["weekly"]["used_pct"], 10)
        j2 = json.dumps({"event": "result", "response": "Daily: 10% used\nWeekly: 70% remaining, resets Mon 09:00"})
        w2 = self.w(j2)
        self.assertEqual(w2["daily"]["used_pct"], 10); self.assertEqual(w2["weekly"]["remaining_pct"], 70); self.assertEqual(w2["weekly"]["resets_text"], "Mon 09:00")
        j3 = json.dumps({"limits": [{"label": "weekly credits", "used": 25, "limit": 100}]})
        self.assertEqual(self.w(j3)["weekly"]["used_pct"], 25)

    def test_text_shapes_and_units(self):
        w = self.w("┃ Daily quota   ▕███░░▏ 38% used · resets in 5h 10m\n┃ Weekly quota  91% remaining (resets Mon 09:00)\n5-hour window: 20% used")
        self.assertEqual((w["daily"]["used_pct"], w["daily"]["remaining_pct"]), (38.0, 62.0))
        self.assertEqual(w["daily"]["resets_text"], "in 5h 10m")
        self.assertEqual((w["weekly"]["remaining_pct"], w["weekly"]["used_pct"]), (91.0, 9.0))
        self.assertIn("5-hour", w)

    def test_never_invents_figures(self):
        self.assertEqual(limits.parse("Session credits used: 12.4 AIU. No limits apply to your plan."), [])  # no percent: nothing fabricated
        self.assertEqual(limits.parse(""), [])
        amb = self.w("Weekly: 40%")  # ambiguous: neither used nor remaining is claimed
        self.assertIsNone(amb["weekly"]["used_pct"]); self.assertIsNone(amb["weekly"]["remaining_pct"]); self.assertIn("40%", amb["weekly"]["detail"])
        self.assertIn("40%", limits.label(amb["weekly"]))


class TestLimitCollection(WorkerCase):
    def test_refresh_stores_windows_raw_and_redacts(self):
        r = self.repo(); db = r.db(); paths = Paths(r.root)
        os.environ["FAKE_USAGE"] = "Daily: 38% used · resets in 5h\nWeekly: 12% used · resets Mon 09:00\ntoken ghp_" + "a" * 36
        res = limits.refresh(db, "antigravity")
        self.assertTrue(res["ok"]); self.assertEqual({w["window"] for w in res["windows"]}, {"daily", "weekly"})
        lim = snapshot.build(paths)["limits"]["antigravity"]
        self.assertEqual({w["window"]: round(w["remaining_pct"]) for w in lim["windows"]}, {"daily": 62, "weekly": 88})
        self.assertNotIn("ghp_", lim["raw"])  # secrets never stored
        self.assertIn("/usage", lim["command"])

    def test_cli_that_reports_nothing_or_is_missing_is_shown_honestly(self):
        r = self.repo(); db = r.db(); paths = Paths(r.root)
        os.environ["FAKE_USAGE"] = "You are on the Pro plan. Session credits: 3.2."
        limits.refresh(db, "copilot")
        c = snapshot.build(paths)["limits"]["copilot"]
        self.assertEqual((c["windows"], c["ok"]), ([], True)); self.assertIn("Pro plan", c["raw"])
        os.environ["MESH_BIN_ANTIGRAVITY"] = "/nonexistent"
        limits.refresh(db, "antigravity")
        a = snapshot.build(paths)["limits"]["antigravity"]
        self.assertFalse(a["ok"]); self.assertIn("not installed", a["error"])

    def test_limits_command_opts_in_and_conductor_collects_in_background(self):
        r = self.repo(); os.environ["MESH_ROOT"] = str(r.root); paths = Paths(r.root)
        os.environ["FAKE_USAGE"] = "Daily: 50% used\nWeekly: 25% used\n"
        cond = conductor.Conductor(paths); self.addCleanup(cond.close)
        self.assertFalse(core.load_config(paths)["limits"]["enabled"])  # off until the user asks once
        runs.enqueue(cond.db, "refresh_limits", {}, "human")
        cond.tick()
        self.assertTrue(core.load_config(paths)["limits"]["enabled"])
        self.assertTrue(wait_for(lambda: (cond.tick() or True) and snapshot.build(paths)["limits"]["copilot"]["windows"], 30))
        self.assertTrue(snapshot.build(paths)["limits"]["antigravity"]["windows"])
        self.assertEqual(len([k for k in cond.futs if k[0] == "limits"]), 0)  # jobs harvested even with no active run


if __name__ == "__main__":
    unittest.main()


class TestLimitsCli(WorkerCase):
    def test_mesh_limits_refresh_prints_windows_and_saves_raw(self):
        r = self.repo(); os.environ["MESH_ROOT"] = str(r.root)
        os.environ["FAKE_USAGE"] = "Daily: 38% used · resets in 5h\nWeekly: 12% used · resets Mon 09:00\n"
        out = r.mesh("limits", "--refresh", "--out", str(r.root / "raw"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("daily · 62% left · resets in 5h", out.stdout); self.assertIn("weekly · 88% left", out.stdout)
        self.assertTrue((r.root / "raw" / "antigravity.usage.raw.txt").exists())
