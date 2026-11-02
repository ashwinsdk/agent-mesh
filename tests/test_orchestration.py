"""Event-driven comms, mailbox injection, verification gates, delegation approvals."""
import json
import os
import threading
import time
import unittest
from pathlib import Path

from helpers import MeshCase, sh
from test_workers import WorkerCase, wait_for
from mesh import bus, core, eventlog, hooks, tasks, verify, workers
from mesh.db import MeshError


class TestComms(WorkerCase):
    def _post(self, r, cli, sid_native, event="post_tool"):
        payload = {"session_id": sid_native, "sessionId": sid_native, "conversationId": sid_native, "cwd": str(r.root), "tool_name": "Bash",
                   "toolCall": {"name": "run_command", "args": {}}}
        return hooks.handle(cli, event, payload, r.root)

    def test_mailbox_injected_once_then_retried_then_failed(self):
        r = self.repo(); db = r.db()
        sid = r.session("claude", "claude0001")
        m = bus.send(db, "human", sid, "question", "which auth header?", None, 1, requires_ack=True)
        out, _ = self._post(r, "claude", "claude0001")
        ctx = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse"); self.assertIn("which auth header?", ctx["additionalContext"])
        self.assertEqual(self._post(r, "claude", "claude0001")[0], "")  # not repeated
        self.assertEqual(db.one("SELECT delivery_state FROM messages WHERE id=?", (m,))["delivery_state"], "delivered")
        # no ack -> retry re-surfaces it
        self.assertEqual(bus.sweep(db, retry_after=0)["retried"], 1)
        self.assertIn("which auth header?", json.loads(self._post(r, "claude", "claude0001")[0])["hookSpecificOutput"]["additionalContext"])
        for _ in range(4):
            bus.sweep(db, retry_after=0)
        self.assertEqual(db.one("SELECT delivery_state FROM messages WHERE id=?", (m,))["delivery_state"], "failed")
        self.assertTrue(db.q("SELECT 1 FROM events WHERE type='msg.delivery_failed'"))
        # ack stops everything
        m2 = bus.send(db, "human", sid, "info", "x", None, 1, requires_ack=True)
        bus.ack(db, sid, m2)
        self.assertEqual(bus.sweep(db, retry_after=0), {"retried": 0, "failed": 0})

    def test_expiry_and_duplicate_protection(self):
        r = self.repo(); db = r.db(); sid = r.session("claude", "claude0001")
        m = bus.send(db, "a", sid, "info", "ttl", None, 2, requires_ack=True, ttl=-1)
        bus.sweep(db)
        self.assertEqual(db.one("SELECT delivery_state FROM messages WHERE id=?", (m,))["delivery_state"], "failed")
        self.assertIsNotNone(bus.send(db, "a", sid, "info", "d", dedupe_key="k"))
        self.assertIsNone(bus.send(db, "a", sid, "info", "d", dedupe_key="k"))

    def test_per_cli_injection_formats(self):
        r = self.repo(); db = r.db()
        c, p, g = r.session("claude", "claude0001"), r.session("copilot", "cop0000001"), r.session("antigravity", "agy000001")
        for s in (c, p, g):
            bus.send(db, "human", s, "info", f"hello {s}", None, 1)
        self.assertIn("additionalContext", json.loads(self._post(r, "copilot", "cop0000001")[0]))
        self.assertEqual(self._post(r, "antigravity", "agy000001", "post_tool")[0], "")  # agy injects only at PreInvocation
        inj = json.loads(self._post(r, "antigravity", "agy000001", "pre_invocation")[0])["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn(f"hello {g}", inj)

    def test_orchestrator_gets_event_digest_at_its_tool_boundary(self):
        r = self.repo(); db = r.db()
        orch = core.register_session(db, "claude", "orchestrator", "orchsess1", os.getpid(), None, role="orchestrator", session_id="orch-t1")
        os.environ["MESH_SESSION"] = orch
        t = tasks.create(db, "work", files=["w/**"]); w = r.session("copilot", "cop0000001")
        tasks.claim(db, t, w); tasks.transition(db, t, "IN_PROGRESS", w); tasks.transition(db, t, "REVIEW", w)
        bus.send(db, w, "orchestrator", "question", "need schema", t, 1, requires_ack=True)
        out, _ = hooks.handle("claude", "post_tool", {"session_id": "orchsess1", "cwd": str(r.root), "tool_name": "Bash"}, r.root)
        txt = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("need schema", txt); self.assertIn("task.transition", txt)
        self.assertEqual(hooks.handle("claude", "post_tool", {"session_id": "orchsess1", "cwd": str(r.root), "tool_name": "Bash"}, r.root)[0], "")

    def test_wait_wakes_fast_ignores_noise_and_own_events(self):
        r = self.repo(); db = r.db()
        orch = core.register_session(db, "claude", "orchestrator", "o", os.getpid(), None, role="orchestrator", session_id="orch-t2")
        eventlog.set_cursor(db, "orchestrator", eventlog.max_seq(db))
        db.event(orch, "task.transition", "T", {"to": "REVIEW"})  # own action: ignored
        db.event("w-1", "tool", None, {"tool": "Bash"})  # noise
        got = {}
        def waiter():
            d2 = r.db(); t0 = time.time()
            got["rows"] = eventlog.wait(d2, "orchestrator", 20, actor=orch); got["dt"] = time.time() - t0
        th = threading.Thread(target=waiter); th.start()
        time.sleep(1.0)
        db.event("w-1", "worker.stalled", "T", {"idle_seconds": 99})
        th.join(25)
        self.assertEqual([x["type"] for x in got["rows"]], ["worker.stalled"])
        self.assertLess(got["dt"], 2.6)
        d3 = r.db()
        self.assertEqual(eventlog.wait(d3, "orchestrator", 0.5, actor=orch), [])  # cursor advanced; nothing new
        # idle CPU: waiting 2s should burn almost no CPU
        c0 = time.process_time(); eventlog.wait(d3, "orchestrator", 2.0, actor=orch)
        self.assertLess(time.process_time() - c0, 0.25)

    def test_wait_cli_end_to_end(self):
        r = self.repo(); db = r.db()
        db.event("w-9", "worker.failed", "T-1", {"why": "boom"})
        out = r.mesh("wait", "--timeout", "3")
        self.assertIn("worker.failed", out.stdout)
        self.assertIn("no new events", r.mesh("wait", "--timeout", "1").stdout)


class GateCase(WorkerCase):
    def staged(self, r, db, title="feat", files=("feat/**",), verify_cmd="", **kw):
        """A task with a claimed worker session and worktree, no process (we play the worker by hand)."""
        t = tasks.create(db, title, files=list(files), verify_cmd=verify_cmd, **kw)
        w = workers.spawn(db, t, cli="claude", start=False)
        tasks.transition(db, t, "IN_PROGRESS", w["id"])
        return t, w, Path(w["worktree"])

    def commit(self, r, wt, name="feat/a.txt", content="x\n"):
        (wt / name).parent.mkdir(parents=True, exist_ok=True); (wt / name).write_text(content)
        r.git("add", "-A", cwd=wt); r.git("commit", "-qm", "w", cwd=wt)


class TestGates(GateCase):
    def test_full_gate_flow_and_dependency_routing(self):
        r = self.proj(); db = r.db()
        up, w, wt = self.staged(r, db, "3D viewer", ["viewer/**"], verify_cmd="test -f viewer/v.js", agent="threed")
        down = tasks.create(db, "page", files=["page/**"], deps=[up], agent="frontend")
        self.commit(r, wt, "viewer/v.js", "export {}\n")
        with self.assertRaises(MeshError):  # approve before submit
            verify.approve(db, up, "orch")
        verify.submit(db, up, w["id"], "viewer exposes mountViewer(el)")
        self.assertEqual(tasks.get(db, up)["status"], "REVIEW")
        with self.assertRaises(MeshError) as cm:
            verify.approve(db, up, "orch")
        self.assertIn("no verification", str(cm.exception))
        v = verify.verify(db, up, "orch")
        self.assertTrue(v["ok"], v["detail"]); self.assertFalse(v["weak"])
        verify.approve(db, up, "orch")
        self.assertEqual(tasks.get(db, up)["status"], "COMPLETED")
        self.assertEqual(tasks.get(db, down)["status"], "READY")
        self.assertEqual(workers.get(db, w["id"])["state"], "TERMINATED")  # verified -> session ended
        self.assertTrue(db.q("SELECT 1 FROM events WHERE type='task.approved'"))
        # downstream worker prompt carries the producer's handoff automatically
        w2 = workers.spawn(db, down, cli="claude", start=False)
        prompt = (Path(w2["run_dir"]) / "prompt.md").read_text()
        self.assertIn("mountViewer(el)", prompt); self.assertIn("3D viewer", prompt)

    def test_failing_verify_blocks_and_reject_restarts_worker_with_feedback(self):
        r = self.proj(); db = r.db()
        self.scenario({"attempts": [[{"write": "feat/a.txt", "content": "v1"}, {"commit": True}, {"submit": True}],
                                    [{"write": "feat/b.txt", "content": "fixed"}, {"commit": True}, {"submit": True}]]})
        t = tasks.create(db, "feat", files=["feat/**"], verify_cmd="test -f feat/b.txt")
        w = workers.spawn(db, t, cli="claude")
        self.assertTrue(self.done(r, w["id"]))
        self.assertEqual(tasks.get(db, t)["status"], "REVIEW")
        v = verify.verify(db, t, "orch")
        self.assertFalse(v["ok"])
        with self.assertRaises(MeshError):
            verify.approve(db, t, "orch")
        verify.reject(db, t, "orch", "feat/b.txt is missing")
        self.assertTrue(wait_for(lambda: tasks.get(db, t)["status"] == "REVIEW" and workers.get(db, w["id"])["state"] == "COMPLETED", 40),
                        (dict(tasks.get(db, t)), dict(workers.get(db, w["id"]))))
        v = verify.verify(db, t, "orch"); self.assertTrue(v["ok"], v["detail"])
        verify.approve(db, t, "orch")
        self.assertEqual(tasks.get(db, t)["status"], "COMPLETED")

    def test_ownership_violation_and_weak_verification_and_moved_head(self):
        r = self.proj(); db = r.db()
        t, w, wt = self.staged(r, db, "own", ["own/**"])
        self.commit(r, wt, "own/ok.txt"); self.commit(r, wt, "elsewhere/oops.txt")
        verify.submit(db, t, w["id"], "")
        v = verify.verify(db, t, "orch")
        self.assertFalse(v["ok"]); self.assertIn("OWNERSHIP VIOLATION", v["detail"]); self.assertIn("elsewhere/oops.txt", v["detail"])
        # weak: nothing configured to run
        t2, w2, wt2 = self.staged(r, db, "weak", ["weak/**"])
        self.commit(r, wt2, "weak/a.txt"); verify.submit(db, t2, w2["id"], "")
        r.mesh("task", "list")
        db.conn.execute("UPDATE tasks SET verify_cmd=''")  # ensure none
        from mesh.core import set_toml_section
        set_toml_section(db.paths.config, "validation", {"test": "", "lint": ""})
        v = verify.verify(db, t2, "orch")
        self.assertTrue(v["ok"] and v["weak"])
        with self.assertRaises(MeshError) as cm:
            verify.approve(db, t2, "orch")
        self.assertIn("WEAK", str(cm.exception))
        # moved HEAD invalidates verification
        self.commit(r, wt2, "weak/late.txt")
        with self.assertRaises(MeshError) as cm:
            verify.approve(db, t2, "orch", accept_unverified=True)
        self.assertIn("moved", str(cm.exception))
        verify.verify(db, t2, "orch"); verify.approve(db, t2, "orch", accept_unverified=True)
        self.assertEqual(tasks.get(db, t2)["status"], "COMPLETED")

    def test_high_risk_needs_independent_review(self):
        r = self.proj(); db = r.db()
        t, w, wt = self.staged(r, db, "auth", ["auth/**"], verify_cmd="true", risk="high")
        self.commit(r, wt, "auth/login.py"); verify.submit(db, t, w["id"], "")
        verify.verify(db, t, "orch")
        with self.assertRaises(MeshError) as cm:
            verify.approve(db, t, "orch")
        self.assertIn("independent", str(cm.exception))
        with self.assertRaises(MeshError):  # author cannot review itself
            verify.review(db, t, w["id"], True)
        verify.review(db, t, "orch", True, "looks fine")  # the orchestrator's own review does not count
        with self.assertRaises(MeshError):
            verify.approve(db, t, "orch")
        verify.review(db, t, "human", True, "security ok")
        verify.approve(db, t, "orch")
        self.assertEqual(tasks.get(db, t)["status"], "COMPLETED")

    def test_worker_cannot_approve_integrate_spawn_or_halt(self):
        r = self.proj(); db = r.db()
        t, w, wt = self.staged(r, db, "g", ["g/**"], verify_cmd="true")
        self.commit(r, wt, "g/a.txt"); verify.submit(db, t, w["id"], "")
        for cmd in (("approve", t), ("verify", t), ("reject", t, "--reason", "x"), ("integrate", t), ("halt",), ("promote",),
                    ("worker", "spawn", "--task", t)):
            res = r.mesh(*cmd, "--as", w["id"])
            self.assertNotEqual(res.returncode, 0, cmd); self.assertIn("worker session", res.stderr, cmd)
        self.assertEqual(tasks.get(db, t)["status"], "REVIEW")

    def test_submit_requires_clean_committed_worktree_and_ownership(self):
        r = self.proj(); db = r.db()
        t, w, wt = self.staged(r, db, "s", ["s/**"])
        (wt / "s").mkdir(); (wt / "s" / "x.txt").write_text("uncommitted")
        with self.assertRaises(MeshError) as cm:
            verify.submit(db, t, w["id"], "")
        self.assertIn("uncommitted", str(cm.exception))
        with self.assertRaises(MeshError):
            verify.submit(db, t, "w-intruder", "")

    def test_integrate_after_approve(self):
        r = self.proj(); db = r.db()
        t, w, wt = self.staged(r, db, "i", ["i/**"], verify_cmd="true")
        self.commit(r, wt, "i/a.txt"); verify.submit(db, t, w["id"], "")
        verify.verify(db, t, "orch"); out = r.mesh("approve", t, "--integrate")
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        self.assertIn("merged into mesh/integration", out.stdout)


class TestDelegation(GateCase):
    def test_spawn_request_needs_orchestrator_approval_and_respects_depth(self):
        r = self.proj(); db = r.db()
        self.scenario([{"sleep": 30}])
        t, w, wt = self.staged(r, db, "parent", ["p/**"])
        sub = tasks.create(db, "sub", files=["sub/**"], parent=t)
        res = r.mesh("worker", "request", "--task", sub, "--agent", "qa", "--reason", "need tests", "--as", w["id"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(db.q("SELECT 1 FROM messages WHERE type='spawn_request' AND recipient='role:orchestrator'"))
        self.assertEqual(tasks.get(db, sub)["status"], "READY")  # nothing spawned without approval
        res = r.mesh("worker", "approve-spawn", "1", "--as", w["id"])
        self.assertNotEqual(res.returncode, 0)  # a worker cannot approve its own request
        res = r.mesh("worker", "approve-spawn", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        child = db.one("SELECT * FROM workers WHERE task_id=?", (sub,))
        self.assertEqual((child["depth"], child["parent_worker"]), (1, w["id"]))
        self.kill_all(r)

    def test_decision_ratification_broadcasts_and_records_adr(self):
        r = self.repo(); db = r.db()
        w = r.session("copilot", "cop0000001")
        m = bus.send(db, w, "orchestrator", "decision", "use SQLite for local storage", "T-1", 1, requires_ack=True)
        res = r.mesh("decide", m, "accept", "--note", "approved")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(list((r.root / ".mesh/memory/decisions").glob("ADR-*.md")))
        self.assertTrue(db.q("SELECT 1 FROM messages WHERE type='decision_result' AND recipient='*' AND content LIKE 'RATIFIED%'"))


if __name__ == "__main__":
    unittest.main()
