"""Drives the real Textual app headlessly (Textual's pilot). Skipped automatically when textual is not installed."""
import asyncio
import json
import os
import time
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

from helpers import MeshCase
from test_workers import WorkerCase, wait_for
from test_conductor import PLAN, OWNED
from mesh import conductor, core, runs, tasks, workers
from mesh.db import DB, Paths

if HAVE:
    from mesh.app import ui


async def until(pilot, fn, timeout=60.0, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        await pilot.pause(step)
    return fn()


@unittest.skipUnless(HAVE, "textual not installed")
class AppCase(WorkerCase, unittest.IsolatedAsyncioTestCase):
    def project(self, setup_done=True):
        r = self.proj()
        os.environ["MESH_ROOT"] = str(r.root)
        os.environ["FAKE_PLAN"] = json.dumps(PLAN)
        self.scenario(OWNED)
        if setup_done:
            core.set_toml_section(r.root / ".mesh" / "mesh.toml", "app", {"cli": "claude"})
        self.paths = Paths(r.root)
        self.addCleanup(self._cleanup)
        return r

    def _cleanup(self):
        try:
            conductor.stop_daemon(self.paths)
            db = DB(self.paths)
            for w in workers.list_workers(db, active_only=True):
                workers.terminate(db, w["id"], "test")
            db.close()
        except Exception:
            pass

    @staticmethod
    def plain(widget):
        c = widget.content
        return c.plain if hasattr(c, "plain") else str(c)


@unittest.skipUnless(HAVE, "textual not installed")
class TestLayout(AppCase):
    async def test_renders_at_small_and_large_sizes_without_errors(self):
        r = self.project()
        for size in ((80, 24), (120, 40), (200, 50)):
            app = ui.MeshApp(self.paths)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(0.6)
                right = app.query_one("#right")
                self.assertEqual(right.display, size[0] >= 100, size)  # narrow terminals fall back to the single-column view
                self.assertIn("agent-mesh", app.top_str)
                self.assertIn("no run yet", app.top_str)
                self.assertIsNotNone(app.query_one("#prompt"))
                self.assertTrue(app.query_one("#prompt").has_focus)

    async def test_help_and_usage_screens_open_and_close(self):
        r = self.project()
        app = ui.MeshApp(self.paths)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.4)
            app.query_one("#prompt").value = "/help"
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: isinstance(app.screen, ui.TextScreen), 10))
            await pilot.press("escape")
            self.assertTrue(await until(pilot, lambda: not isinstance(app.screen, ui.TextScreen), 5))
            app.query_one("#prompt").value = "/usage"
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: isinstance(app.screen, ui.TextScreen), 10))
            await pilot.press("escape")
            app.query_one("#prompt").value = "/wat"
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: any("Unknown command" in l for l in app.activity_lines), 10))


@unittest.skipUnless(HAVE, "textual not installed")
class TestSetupWizard(AppCase):
    async def test_first_run_shows_wizard_and_requires_acceptance(self):
        r = self.project(setup_done=False)
        core.set_toml_section(Paths(r.root).config, "autonomy", {"mode": "worktree-confined", "accepted_by": "", "accepted_at": "", "accepted_cli_flags": "", "plan_approval": True})
        app = ui.MeshApp(self.paths)
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause(0.6)
            self.assertIsInstance(app.screen, ui.SetupScreen)
            await pilot.click("#s-save")
            self.assertTrue(await until(pilot, lambda: "accepted" in self.plain(app.screen.query_one("#s-msg")), 10))
            self.assertIsInstance(app.screen, ui.SetupScreen)  # not saved without acceptance
            app.screen.query_one("#s-accept").value = True
            await pilot.pause(0.6)  # let the button's press animation finish before clicking again
            await pilot.click("#s-save")
            self.assertTrue(await until(pilot, lambda: not isinstance(app.screen, ui.SetupScreen), 30))
            self.assertFalse(__import__("mesh.app.setup", fromlist=["x"]).needs_setup(self.paths))


@unittest.skipUnless(HAVE, "textual not installed")
class TestNeedsYou(AppCase):
    async def test_strip_details_and_function_keys(self):
        r = self.project(); db = r.db()
        rid = runs.create_run(db, "x", "claude")
        aid = runs.add_attention(db, rid, "plan_approval", str(rid), "Plan ready: 4 tasks", "task list here")
        app = ui.MeshApp(self.paths)
        async with app.run_test(size=(140, 40)) as pilot:
            self.assertTrue(await until(pilot, lambda: "Plan ready" in app.needs_str, 10))
            self.assertTrue(app.query_one("#needs").display)
            # the strip is a normal focus stop: Tab from the input reaches it, and its own keys then work (no function keys anywhere)
            await pilot.press("tab")
            self.assertTrue(await until(pilot, lambda: app.query_one("#needs").has_focus, 5), app.focused)
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: isinstance(app.screen, ui.DetailsScreen), 5))
            await pilot.press("escape")
            await pilot.press("a")
            self.assertTrue(await until(pilot, lambda: db.q("SELECT 1 FROM commands WHERE kind='approve_item'"), 10))

    async def test_hidden_when_nothing_needs_you(self):
        r = self.project()
        app = ui.MeshApp(self.paths)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.8)
            self.assertFalse(app.query_one("#needs").display)


@unittest.skipUnless(HAVE, "textual not installed")
class TestAgentView(AppCase):
    async def test_agent_screen_shows_readable_output(self):
        r = self.project(); db = r.db()
        t = tasks.create(db, "Luffy model", files=["l/**"])
        w = workers.spawn(db, t, cli="claude", start=False)
        d = Path(w["run_dir"]); d.mkdir(parents=True, exist_ok=True)
        (d / "stream.jsonl").write_text("\n".join(json.dumps(x) for x in [
            {"type": "system", "subtype": "init", "session_id": "s1", "model": "m"},
            {"type": "assistant", "message": {"model": "m", "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "l/luffy.js"}}], "usage": {"input_tokens": 1, "output_tokens": 1}}},
            {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "Luffy is done"}], "usage": {"input_tokens": 1, "output_tokens": 1}}}]) + "\n")
        workers.set_state(db, w["id"], "WORKING", None)
        app = ui.MeshApp(self.paths)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.5)
            app.query_one("#prompt").value = f"/agent {w['id']}"
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: isinstance(app.screen, ui.AgentScreen), 10))
            await pilot.pause(1.2)
            head = self.plain(app.screen.query_one("#agent-head"))
            self.assertIn("working", head); self.assertIn("claude", head)
            log = app.screen.query_one("#agent-log")
            text = "\n".join(l.text for l in log.lines)
            self.assertIn("Write l/luffy.js", text); self.assertIn("Luffy is done", text)
            self.assertNotIn("session_id", text)  # readable, not raw JSON
            await pilot.press("escape")
            self.assertFalse(isinstance(app.screen, ui.AgentScreen))
        workers.terminate(db, w["id"], "test")


@unittest.skipUnless(HAVE, "textual not installed")
class TestEndToEndThroughTheUI(AppCase):
    async def test_type_a_prompt_approve_the_plan_and_watch_it_finish(self):
        r = self.project(); db = r.db()
        app = ui.MeshApp(self.paths)
        seen = {"agents": 0, "glyphs": set()}
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.5)
            app.query_one("#prompt").value = "Build a One Piece 3D site"
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: "Plan ready" in app.needs_str, 60), (app.top_str, app.activity_lines[-5:]))
            self.assertIn("waiting for you", app.top_str)
            self.assertEqual(sum("Build a One Piece 3D site" in l for l in app.activity_lines), 1)  # echoed once, not twice
            self.assertEqual(len([t for t in app.task_view if str(t[0]).startswith("T-")]), 5)  # epic + 4 tasks listed hierarchically
            await pilot.press("tab")  # Tab from the input to the Needs-you strip, then 'a' approves the plan
            await pilot.press("a")
            def progress():
                seen["agents"] = max(seen["agents"], len([a for a in app.agent_view if a[2].plain != "Orchestrator"]))
                return "complete" in app.top_str
            self.assertTrue(await until(pilot, progress, 170, 0.5), (app.top_str, app.activity_lines[-8:]))
            self.assertGreaterEqual(seen["agents"], 1)  # workers appeared in the agents pane
            text = "\n".join(app.activity_lines)
            for n in ("plan the work", "Plan is ready", "Executing the plan", "Started frontend", "approved", "merged into the integration branch", "Run complete"):
                self.assertIn(n, text)
            self.assertTrue(await until(pilot, lambda: "promote" in app.needs_str.lower() or "Promote" in app.needs_str, 20))
            self.assertIn("✓", "".join(t[1].plain for t in app.task_view))  # completed tasks show a tick
            self.assertIn("100%", app.top_str)
        # leaving the app does not stop the work: the conductor survives and re-attaching shows the same state
        self.assertTrue(conductor.status(self.paths)["running"])
        app2 = ui.MeshApp(self.paths)
        async with app2.run_test(size=(120, 40)) as pilot2:
            await pilot2.pause(0.8)
            self.assertIn("complete", app2.top_str)
            self.assertTrue(any("Run complete" in l for l in app2.activity_lines))

    async def test_orchestrator_is_one_row_idle_sessions_stay_visible_until_killed_or_cleared(self):
        r = self.project(); db = r.db()
        ghost = r.session("antigravity", "ghost0001")
        db.conn.execute("UPDATE sessions SET last_seen=? WHERE id=?", (time.time() - 99999, ghost))
        for i in range(3):
            core.register_session(db, "copilot", "orchestrator", None, os.getpid(), None, role="orchestrator", session_id=f"orch-{i}")
        core.mark_stale(db, 1800)
        app = ui.MeshApp(self.paths)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.8)
            names = [a[2].plain for a in app.agent_view]
            self.assertEqual(names.count("Orchestrator"), 1)  # retired orchestrators are gone
            row = next(a for a in app.agent_view if a[0] == ghost)
            self.assertEqual(row[5].plain, "idle")  # a silent session is IDLE and still listed, not hidden
            self.assertIn("1 idle", app.sessions_title_str)
            # kill it from the Sessions table with the 'k' key
            app.query_one("#agents").focus()
            keys = [a[0] for a in app.agent_view]
            app.query_one("#agents").move_cursor(row=keys.index(ghost))
            await pilot.press("k")
            self.assertTrue(await until(pilot, lambda: db.q("SELECT 1 FROM commands WHERE kind='kill_session'"), 10))
            conductor.start_daemon(self.paths)
            self.assertTrue(await until(pilot, lambda: all(a[0] != ghost for a in app.agent_view), 30))  # gone from the list
            self.assertTrue(any("killed" in l for l in app.activity_lines))  # and the user is told


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE, "textual not installed")
class TestRevamp(AppCase):
    PALETTE = {"#090909", "#171717", "#262626", "#E6E6E6", "#A3A3A3", "#65E572", "#618BFF", "#BD83FF", "#63D6E5", "#E7BD72", "#FF6B6B"}

    async def test_midnight_80s_palette_is_applied(self):
        r = self.project()
        app = ui.MeshApp(self.paths)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.5)
            self.assertEqual(app.theme, "midnight80s")
            t = ui.MIDNIGHT
            self.assertEqual((t.background.upper(), t.surface.upper(), t.panel.upper(), t.foreground.upper()), ("#090909", "#171717", "#262626", "#E6E6E6"))
            self.assertEqual((t.success.upper(), t.primary.upper(), t.secondary.upper(), t.accent.upper(), t.warning.upper(), t.error.upper()),
                             ("#65E572", "#618BFF", "#BD83FF", "#63D6E5", "#E7BD72", "#FF6B6B"))
            # the screen really renders on the midnight background, and every colour the view-models use comes from the palette
            self.assertEqual(app.screen.styles.background.hex.upper()[:7], "#090909")
            used = {c.upper() for c in [v[1] for v in ui.STATE_GLYPH.values()] + [v[1] for v in ui.TASK_GLYPH.values()]}
            self.assertTrue(used <= self.PALETTE, used - self.PALETTE)
            self.assertEqual(app.query_one("#activity").styles.background.hex.upper()[:7], "#171717")

    async def test_no_function_keys_anywhere(self):
        import re
        r = self.project()
        app = ui.MeshApp(self.paths)
        screens = [ui.TextScreen("x", lambda s: ""), ui.AgentScreen("w"), ui.TaskScreen("T"), ui.DetailsScreen({"title": "", "detail": "", "id": 1}), ui.SetupScreen()]
        widgets = [ui.SessionTable, ui.TaskTable, ui.NeedsYou]
        keys = [b.key for b in app.BINDINGS] + [b.key for s in screens for b in s.BINDINGS] + [b.key for w in widgets for b in w.BINDINGS]
        self.assertFalse([k for k in keys if re.fullmatch(r"f\d+", k)], keys)
        from mesh.app import commands
        self.assertNotRegex(ui.render_help({}).renderables[0].plain, r"\bF\d+\b")

    async def test_prompt_bar_is_fully_visible_and_not_overlapped_by_the_footer(self):
        r = self.project()
        for size in ((80, 24), (120, 30), (160, 45)):
            app = ui.MeshApp(self.paths)
            async with app.run_test(size=size) as pilot:
                await pilot.pause(0.6)
                p, f, b = app.query_one("#prompt").region, app.query_one("Footer").region, app.query_one("#body").region
                self.assertGreaterEqual(p.height, 3, size)  # border + text line + border all drawn
                self.assertGreaterEqual(p.y, b.y + b.height, f"prompt must start below the body {size}")
                self.assertLessEqual(p.y + p.height, f.y, f"prompt must end above the footer {size}: {p} vs {f}")
                self.assertGreaterEqual(f.height, 1)
                self.assertLessEqual(f.y + f.height, size[1])

    async def test_usage_panel_shows_daily_and_weekly_limits_and_honest_placeholders(self):
        r = self.project(); db = r.db()
        app = ui.MeshApp(self.paths)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(0.8)
            self.assertIn("not fetched yet", app.usage_str)  # nothing invented before the CLI has been asked
            self.assertIn("/limits", app.usage_str)
            with db.tx() as c:
                for cli, win, rem, reset in (("antigravity", "daily", 62, "in 5h"), ("antigravity", "weekly", 88, "Mon 09:00"), ("copilot", "weekly", 8, "Mon 09:00")):
                    c.execute("INSERT INTO limits(ts,cli,window,used_pct,remaining_pct,resets_text,detail,source) VALUES(?,?,?,?,?,?,?,?)",
                              (time.time(), cli, win, 100 - rem, rem, reset, "", "agy -p /usage"))
                    c.execute("INSERT INTO limit_raw(cli,ts,ok,raw,error,command) VALUES(?,?,1,'raw',NULL,'x') ON CONFLICT(cli) DO UPDATE SET ts=excluded.ts", (cli, time.time()))
            self.assertTrue(await until(pilot, lambda: "62% left" in app.usage_str, 10), app.usage_str)
            for needle in ("daily", "weekly", "88% left", "resets in 5h", "resets Mon 09:00", "8% left"):
                self.assertIn(needle, app.usage_str)
            app.query_one("#prompt").value = "/limits"
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: db.q("SELECT 1 FROM commands WHERE kind='refresh_limits'"), 10))

    async def test_clear_done_session_with_c_and_live_one_is_refused(self):
        r = self.project(); db = r.db()
        done = r.session("copilot", "done00001"); core.end_session(db, done)
        live = r.session("claude", "live00001")
        app = ui.MeshApp(self.paths)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.8)
            keys = [a[0] for a in app.agent_view]
            self.assertIn(done, keys); self.assertIn(live, keys)
            self.assertIn("done", [a[5].plain for a in app.agent_view if a[0] == done])
            app.query_one("#agents").focus()
            app.query_one("#agents").move_cursor(row=keys.index(done))
            await pilot.press("c")
            self.assertTrue(await until(pilot, lambda: db.q("SELECT 1 FROM commands WHERE kind='clear_session'"), 10))
            conductor.start_daemon(self.paths)
            self.assertTrue(await until(pilot, lambda: done not in [a[0] for a in app.agent_view], 30))
            self.assertIn(live, [a[0] for a in app.agent_view])
            app.query_one("#agents").move_cursor(row=[a[0] for a in app.agent_view].index(live))
            await pilot.press("c")
            self.assertTrue(await until(pilot, lambda: any("kill it instead" in l for l in app.activity_lines), 30), app.activity_lines[-4:])
            self.assertIn(live, [a[0] for a in app.agent_view])  # still there
