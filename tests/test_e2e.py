"""End-to-end: a scripted orchestrator drives real runner processes (fake CLIs) through plan -> delegate -> supervise -> verify -> integrate."""
import json
import os
import time
import unittest
from pathlib import Path

from helpers import MeshCase
from test_workers import WorkerCase, wait_for
from mesh import eventlog, gitflow, snapshot, tasks, tui, usage, verify, workers
from mesh.db import MeshError


class TestEndToEnd(WorkerCase):
    def test_six_task_graph_with_crash_recovery_completes(self):
        r = self.proj(test_cmd="true"); db = r.db()
        # Scenario: every worker writes a file in its owned dir, commits, submits.
        # The "zoro" worker's FIRST attempt crashes hard (kill -9 style) and must be recovered.
        self.scenario({"attempts": [
            [{"when_title": "zoro", "crash": True}, {"owned_file": True}, {"commit": True}, {"submit": True}],
            [{"owned_file": True}, {"commit": True}, {"submit": True}]]})
        epic = tasks.create(db, "Three.js One Piece site", kind="epic")
        mk = lambda title, files, deps=(), agent="frontend", **kw: tasks.create(db, title, parent=epic, files=files, deps=list(deps), agent=agent,
                                                                       acceptance=f"{title} works", verify_cmd="true", **kw)
        scaffold = mk("scaffold scene", ["app/**"])
        luffy = mk("luffy model", ["models/luffy/**"], [scaffold]); zoro = mk("zoro model", ["models/zoro/**"], [scaffold])
        sanji = mk("sanji model", ["models/sanji/**"], [scaffold]); ui = mk("page ui labels", ["ui/**"], [luffy, zoro, sanji])
        final = mk("integration smoke", ["smoke/**"], [ui], agent="qa")
        order = []
        deadline = time.time() + 150
        all_ids = [scaffold, luffy, zoro, sanji, ui, final]
        while time.time() < deadline:
            workers.reconcile(db)
            for t in tasks.ready(db):
                if t["id"] in all_ids and db.one("SELECT 1 FROM workers WHERE task_id=? AND state NOT IN ('FAILED','TERMINATED')", (t["id"],)) is None:
                    workers.spawn(db, t["id"], cli="claude", actor="orch"); order.append(("spawn", t["title"]))
            for t in db.q("SELECT * FROM tasks WHERE status='REVIEW' AND kind!='epic'"):
                v = verify.verify(db, t["id"], "orch")
                self.assertTrue(v["ok"], v["detail"])
                verify.approve(db, t["id"], "orch")
                res = gitflow.integrate(db, t["id"], "orch")
                self.assertTrue(res["ok"], res)
                order.append(("done", t["title"]))
            if all(tasks.get(db, i)["status"] == "COMPLETED" for i in all_ids):
                break
            eventlog.wait(db, "e2e-orch", 1.0, actor="orch")
        self.assertTrue(all(tasks.get(db, i)["status"] == "COMPLETED" for i in all_ids), [(tasks.get(db, i)["title"], tasks.get(db, i)["status"]) for i in all_ids])
        # dependency order respected: nothing spawned before its upstream was approved
        pos = {e: i for i, e in enumerate(order)}
        for up, down in (("scaffold scene", "luffy model"), ("scaffold scene", "zoro model"), ("luffy model", "page ui labels"),
                         ("zoro model", "page ui labels"), ("sanji model", "page ui labels"), ("page ui labels", "integration smoke")):
            self.assertLess(pos[("done", up)], pos[("spawn", down)], (up, down))
        # the three models ran in parallel (all spawned before any of them finished)
        self.assertLess(max(pos[("spawn", m)] for m in ("luffy model", "zoro model", "sanji model")),
                        min(pos[("done", m)] for m in ("luffy model", "zoro model", "sanji model")) + 3)
        # crash recovery happened exactly for zoro
        zw = db.one("SELECT * FROM workers WHERE task_id=?", (zoro,))
        self.assertEqual(zw["restarts"], 1)
        self.assertTrue(db.q("SELECT 1 FROM checkpoints WHERE task_id=? AND reason='worker-crash'", (zoro,)))
        # everything is on the integration branch; nothing was merged by workers; no overwrites
        iw = db.paths.worktrees / "_integration"
        for d in ("app", "models/luffy", "models/zoro", "models/sanji", "ui", "smoke"):
            self.assertTrue(list((iw / d).glob("*.txt")), d)
        # all workers released; usage recorded for every worker
        self.assertEqual(workers.active_count(db), 0)
        self.assertEqual(len(usage.latest_series(db)), 6)
        # dashboard and report reflect reality
        s = snapshot.build(db.paths)
        self.assertEqual(s["project"]["phase"], "complete"); self.assertAlmostEqual(s["project"]["progress"], 1.0)
        self.assertEqual(s["project"]["integration"]["ok"], 6)
        self.assertEqual(s["usage"]["global"]["api_spend"], 0.0)  # billing_mode unknown -> never counted as spend
        self.assertGreater(s["usage"]["global"]["informational_api_equiv"] + s["usage"]["global"]["unclassified_cost"], 0)
        text = tui.render_all(db.paths)
        for needle in ("Overview", "AGENTS", "SESSIONS", "TASKS", "COMMUNICATION", "USAGE", "PER AGENT", "100%"):
            self.assertIn(needle, text)
        self.assertEqual(r.mesh("report").returncode, 0)

    def test_billing_mode_controls_spend(self):
        r = self.proj(); db = r.db()
        from mesh.core import set_toml_section
        set_toml_section(db.paths.config, "billing", {"claude": "api", "copilot": "subscription", "antigravity": "unknown"})
        t = tasks.create(db, "bill", files=["b/**"])
        w = workers.spawn(db, t, cli="claude")
        self.assertTrue(self.done(r, w["id"]))
        s = snapshot.build(db.paths)
        self.assertAlmostEqual(s["usage"]["per_cli"]["claude"]["api_spend"], 0.01)
        self.assertEqual(s["usage"]["per_cli"]["claude"]["informational_api_equiv"], 0.0)
        # spend cap blocks new spawns only for api-mode spend
        set_toml_section(db.paths.config, "workers", {"max_total_cost_usd": 0.005})
        with self.assertRaises(MeshError) as cm:
            workers.spawn(db, tasks.create(db, "more", files=["m/**"]), cli="claude")
        self.assertIn("spend cap", str(cm.exception))

    def test_up_dry_run_builds_orchestrator_prompt_and_requires_opt_in(self):
        r = self.repo(); os.environ["MESH_ROOT"] = str(r.root)
        res = r.mesh("up", "claude", "Build a 3D bottle site", "--dry-run")
        self.assertNotEqual(res.returncode, 0); self.assertIn("PRE-GRANTED", res.stderr + res.stdout)
        r.mesh("autonomy", "accept", "--yes"); r.mesh("sync"); r.mesh("commit-config")
        res = r.mesh("up", "claude", "Build a 3D bottle site", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        for needle in ("PRIMARY ORCHESTRATOR", "mesh worker spawn", "mesh wait", "mesh verify", "mesh approve", "Build a 3D bottle site", "WAIT for the user"):
            self.assertIn(needle, res.stdout)
        self.assertTrue((r.root / ".mesh/instructions/ORCHESTRATOR.md").exists())
        res = r.mesh("up", "claude", "--dry-run")
        self.assertNotEqual(res.returncode, 0); self.assertIn("project prompt", res.stderr)
        res = r.mesh("up", "claude", "--resume", "--dry-run")
        self.assertEqual(res.returncode, 0); self.assertIn("RESUMING", res.stdout)

    def test_autonomy_accept_records_policy_and_requires_explicit_yes(self):
        r = self.repo(); os.environ["MESH_ROOT"] = str(r.root)
        res = r.mesh("autonomy", "accept")  # non-tty, no --yes
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(r.mesh("autonomy", "accept", "--yes").returncode, 0)
        a = json.loads(r.mesh("autonomy", "show").stdout)
        self.assertTrue(a["accepted_at"]); self.assertIn("worktree-confined", a["mode"]); self.assertIn("allow-all-tools", a["accepted_cli_flags"])


class TestDashboardScale(MeshCase):
    def test_60_worker_snapshot_is_fast_and_complete(self):
        r = self.repo(); db = r.db()
        epic = tasks.create(db, "big", kind="epic")
        t0 = time.time()
        with db.tx() as c:
            for i in range(60):
                tid = f"T{i}"
                c.execute("INSERT INTO tasks(id,parent_id,kind,title,status,created,files,agent,session_id,started,last_activity) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (tid, epic, "task", f"task {i}", "IN_PROGRESS", t0, "[]", "backend", f"w-{i}", t0 - 30, t0))
                c.execute("INSERT INTO workers(id,agent,cli,model,state,task_id,started,last_output,last_event,pid) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (f"w-{i}", "backend", ("claude", "copilot", "antigravity")[i % 3], "m", "WORKING", tid, t0 - 30, t0 - i, t0, 1000 + i))
                c.execute("INSERT INTO sessions(id,cli,agent,started,last_seen,role,worker_id) VALUES(?,?,?,?,?,?,?)", (f"w-{i}", "claude", "backend", t0 - 30, t0, "worker", f"w-{i}"))
                c.execute("INSERT INTO usage(ts,session_id,cli,model,source,worker_id,billing_mode,input_tokens,output_tokens,cost_usd,cost_source,task_id,agent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (t0, f"w-{i}", ("claude", "copilot", "antigravity")[i % 3], "m", "reported", f"w-{i}", "api" if i % 3 == 0 else "subscription", 1000, 200, 0.02, "reported", tid, "backend"))
        t1 = time.perf_counter(); c0 = time.process_time()
        for _ in range(5):
            text = tui.render_all(db.paths)
        per = (time.process_time() - c0) / 5
        self.assertLess(time.perf_counter() - t1, 5.0)
        self.assertLess(per, 0.5, f"{per:.3f}s CPU per full render (60 workers)")
        s = snapshot.build(db.paths)
        self.assertEqual(len(s["agents"]), 60); self.assertEqual(s["usage"]["per_cli"]["claude"]["series"], 20)
        self.assertAlmostEqual(s["usage"]["per_cli"]["claude"]["api_spend"], 0.4)
        self.assertAlmostEqual(s["usage"]["per_cli"]["copilot"]["api_spend"], 0.0)  # subscription -> informational only
        self.assertAlmostEqual(s["usage"]["per_cli"]["copilot"]["informational_api_equiv"], 0.4)
        self.assertEqual(s["usage"]["per_agent"]["backend"]["series"], 60)
        self.assertIn("WORKING", text)

    def test_every_dashboard_field_present_or_marked_unavailable(self):
        r = self.repo(); db = r.db()
        sid = r.session("copilot", "cop0000001", "frontend")
        s = snapshot.build(db.paths)
        a = next(x for x in s["agents"] if x["id"] == sid)
        for k in ("id", "name", "role", "cli", "model", "session", "pid", "task", "state", "started", "duration", "last_activity_age", "progress", "blocker", "latest"):
            self.assertIn(k, a)
        for k in ("totals", "per_cli", "per_model", "list", "restarts_total"):
            self.assertIn(k, s["sessions"])
        for k in ("global", "per_cli", "per_model", "per_agent", "per_task", "per_session", "quota", "rate_limit_events", "history_tokens_by_day", "notes"):
            self.assertIn(k, s["usage"])
        self.assertIsNone(s["usage"]["quota"])  # unavailable, not zero
        text = tui.render_all(db.paths)
        self.assertIn("unavailable", text)
        out = r.mesh("--json", "dash")
        self.assertEqual(json.loads(out.stdout)["version"], 1)
        for tab in ("overview", "agents", "sessions", "tasks", "comms", "usage"):
            self.assertEqual(r.mesh("dash", "--once", "--tab", tab).returncode, 0, tab)


if __name__ == "__main__":
    unittest.main()


class TestWebViewer(MeshCase):
    def test_read_only_localhost_viewer(self):
        import threading, urllib.request, urllib.error
        from mesh import webdash
        r = self.repo(); db = r.db()
        tasks.create(db, "<script>alert(1)</script>", files=["x/**"])
        srv = webdash.make_server(db.paths, 0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{port}"
        snap = json.loads(urllib.request.urlopen(base + "/api/snapshot", timeout=5).read())
        self.assertEqual(snap["version"], 1)
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        self.assertIn("textContent", html); self.assertNotIn("innerHTML", html)  # untrusted text never interpreted as HTML
        for method in ("POST", "PUT", "DELETE"):
            req = urllib.request.Request(base + "/api/snapshot", method=method, data=b"x")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 405)
        req = urllib.request.Request(base + "/api/snapshot", headers={"Host": "evil.example.com"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 403)  # DNS-rebinding guard
        resp = urllib.request.urlopen(base + "/stream", timeout=5)
        first = resp.readline().decode()
        self.assertTrue(first.startswith("data: ")); self.assertEqual(json.loads(first[6:])["version"], 1)
        resp.close()


class TestProbe(WorkerCase):
    def test_probe_reports_parser_coverage_for_each_cli_format(self):
        r = self.repo(); os.environ["MESH_ROOT"] = str(r.root)
        self.scenario([{"write": "hello.txt", "content": "hi"}])
        res = r.mesh("probe", "claude")
        self.assertNotEqual(res.returncode, 0); self.assertIn("--yes", res.stdout)  # refuses without explicit confirmation
        for cli, needles in (("claude", ("native session id found: fake-claude", "token usage in stream: yes", "cost in stream: 0.01")),
                             ("antigravity", ("native session id found: fake-antigravity", "token usage in stream: yes", "cost in stream: NO")),
                             ("copilot", ("native session id found: fake-copilot", "token usage in stream: NO", "AI credits in stream: 0.5000 AIU", "OTel token usage: {'input': 120, 'output': 30"))):
            out = r.root / f"out-{cli}"
            res = r.mesh("probe", cli, "--yes", "--out", str(out))
            self.assertEqual(res.returncode, 0, res.stderr)
            for n in needles:
                self.assertIn(n, res.stdout, (cli, res.stdout))
            self.assertIn("hello.txt created: True", res.stdout)
            self.assertTrue((out / f"{cli}.stream.jsonl").exists())

    def test_probe_hooks_mode_reports_honestly_when_hooks_do_not_fire(self):
        r = self.repo(); os.environ["MESH_ROOT"] = str(r.root)
        self.scenario([{"write": "hello.txt", "content": "none"}])
        res = r.mesh("probe", "claude", "--yes", "--hooks", "--out", str(r.root / "o"))
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("hooks fired", res.stdout); self.assertIn("NO  -> hooks are NOT running", res.stdout)  # the fake CLI runs no hooks
        self.assertIn("agent actually saw the injected message (token written to hello.txt): NO", res.stdout)

    def test_probe_hooks_mode_detects_working_hooks_and_injection_in_every_format(self):
        r = self.repo(); os.environ["MESH_ROOT"] = str(r.root)
        self.scenario([{"hook": "hello.txt"}])
        for cli in ("claude", "copilot", "antigravity"):
            res = r.mesh("probe", cli, "--yes", "--hooks", "--out", str(r.root / f"o-{cli}"))
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            self.assertIn("mailbox message marked delivered by the hook: YES", res.stdout, (cli, res.stdout))
            self.assertIn("agent actually saw the injected message (token written to hello.txt): YES", res.stdout, (cli, res.stdout))
