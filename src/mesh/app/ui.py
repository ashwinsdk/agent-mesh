"""The `mesh` app: a Claude-Code-style terminal UI over the conductor.

Left: conversation/activity timeline + a "Needs you" strip. Right: agents, tasks, usage. Bottom: one input box
(plain text = project prompt / message to the orchestrator; `/` = commands). The UI only READS state (snapshot/feed) and
ENQUEUES commands for the conductor, so it can be closed and reopened at any time without affecting execution.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, RichLog, Select, Static

from .. import adapters, brain, conductor, core, feed, health, runs, snapshot, streams, tasks as tasks_mod, verify
from ..db import DB, MeshError, Paths
from . import commands, setup

STATE_GLYPH = {"WORKING": ("●", "green"), "COMMUNICATING": ("●", "green"), "PLANNING": ("●", "green"), "INITIALISING": ("●", "green"),
               "REVIEWING": ("◐", "yellow"), "THINKING": ("◆", "magenta"), "IDLE": ("◆", "dim"), "BLOCKED": ("⏸", "yellow"),
               "PAUSED": ("⏸", "yellow"), "FAILED": ("✗", "red"), "COMPLETED": ("✓", "dim green"), "TERMINATED": ("■", "dim")}
TASK_GLYPH = {"COMPLETED": ("✓", "green"), "IN_PROGRESS": ("●", "green"), "ASSIGNED": ("●", "green"), "REVIEW": ("◐", "yellow"),
              "READY": ("○", "white"), "BACKLOG": ("○", "dim"), "BLOCKED": ("⏸", "yellow"), "PAUSED": ("⏸", "yellow"),
              "FAILED": ("✗", "red"), "NEEDS_REWORK": ("↺", "yellow"), "CANCELLED": ("–", "dim")}
LEVEL_STYLE = {"info": "white", "ok": "green", "warn": "yellow", "bad": "bold red"}
RUN_LABEL = {"planning": "planning", "awaiting_approval": "waiting for you", "executing": "executing", "paused": "paused",
             "integrating": "final checks", "complete": "complete", "failed": "failed", "halted": "halted"}


def dur(sec) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    return f"{sec}s" if sec < 90 else f"{sec // 60}m" if sec < 5400 else f"{sec / 3600:.1f}h"


def bar(frac: float, w: int = 10) -> str:
    n = int(round(w * max(0.0, min(1.0, frac))))
    return "█" * n + "░" * (w - n)


def short(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


# ====================================================================== view-models (pure, used by widgets and tests)

def top_text(snap: dict, narrow: bool = False) -> Text:
    p, run, cond = snap["project"], snap["run"], snap["conductor"]
    t = Text(" ◆ agent-mesh ", style="bold magenta")
    t.append(f" {p['name']} ", style="bold")
    if not run:
        t.append("  no run yet: describe what you want built below", style="dim")
    else:
        t.append(f"  RUN {run['id']} ", style="dim")
        t.append(RUN_LABEL.get(run["status"], run["status"]), style={"executing": "bold green", "complete": "bold green", "failed": "bold red", "halted": "bold red",
                                                                    "awaiting_approval": "bold yellow", "paused": "bold yellow"}.get(run["status"], "bold"))
        if p["leaf_tasks"]:
            t.append(f"  {bar(p['progress'])} {p['progress'] * 100:.0f}%", style="green")
    states = {}
    for a in snap["agents"]:
        states[a["state"]] = states.get(a["state"], 0) + 1
    work_n = sum(states.get(k, 0) for k in ("WORKING", "COMMUNICATING", "PLANNING", "INITIALISING"))
    t.append(f"  ● {work_n} working", style="green")
    if states.get("REVIEWING"):
        t.append(f"  ◐ {states['REVIEWING']} review", style="yellow")
    g = snap["usage"]["global"]
    if g.get("api_spend"):
        t.append(f"  API ${g['api_spend']:.2f}", style="cyan")
    if g.get("ai_credits"):
        t.append(f"  {g['ai_credits']:.2f} AIU", style="cyan")
    t.append("  conductor ", style="dim")
    t.append("●" if cond["running"] else "○ stopped", style="green" if cond["running"] else "red")
    return t


def agent_rows(snap: dict) -> list[tuple]:
    titles = {t["id"]: t["title"] for t in snap["tasks"]["list"]}
    live, done = [], []
    for a in snap["agents"]:
        g, col = STATE_GLYPH.get(a["state"], ("·", "white"))
        name = "Orchestrator" if a["kind"] == "orchestrator" else (short(titles.get(a["task"]), 24) if a["task"] else (a["name"] or "interactive"))
        role = "" if a["kind"] == "orchestrator" else (a["name"] or "")
        row = (a["id"], Text(g, style=col), Text(name, style="bold" if a["kind"] == "orchestrator" else ""), Text(role, style="dim"),
               Text(a["cli"], style="dim"), Text(a["state"].lower(), style=col), dur(a["duration"]) if a["state"] not in ("IDLE",) else "")
        (done if a["state"] in ("COMPLETED", "TERMINATED", "FAILED") else live).append((a["kind"] != "orchestrator", row))
    live.sort(key=lambda x: x[0])
    return [r for _, r in live] + [r for _, r in done[-6:]]


def task_rows(snap: dict) -> list[tuple]:
    L = snap["tasks"]["list"]
    by_parent = {}
    for x in L:
        by_parent.setdefault(x["parent"], []).append(x)
    rows = []
    def walk(pid, depth):
        for x in by_parent.get(pid, []):
            if x["kind"] == "epic":
                rows.append((x["id"], Text("▾", style="dim"), Text(short(x["title"], 28), style="bold"), Text(""), Text("")))
            else:
                g, col = TASK_GLYPH.get(x["status"], ("·", "white"))
                ver = "" if x["verified"] is None else (" ✓" if x["verified"] else " ✗")
                deps = f" ← {len(x['deps'])}" if x["deps"] and x["status"] in ("BACKLOG",) else ""
                rows.append((x["id"], Text(g, style=col), Text("  " * depth + short(x["title"], 26) + deps), Text(x["status"].lower().replace("_", " "), style=col),
                             Text(dur(x["duration"]) + ver, style="dim")))
            walk(x["id"], depth + 1)
    walk(None, 0)
    return rows


def usage_text(snap: dict) -> Text:
    u = snap["usage"]
    t = Text()
    if not u["per_cli"]:
        return Text("no usage reported yet", style="dim")
    for cli, a in sorted(u["per_cli"].items()):
        tok = (a["input"] or 0) + (a["output"] or 0)
        t.append(f"{cli:<12}", style="bold")
        t.append(f"{snapshot._n(tok) if a['tokens_known'] else 'n/a':>7} tok ")
        if a["ai_credits"] is not None:
            t.append(f"{a['ai_credits']:.2f} AIU", style="cyan")
        elif a["api_spend"]:
            t.append(f"${a['api_spend']:.3f}", style="cyan")
        elif a["informational_api_equiv"]:
            t.append(f"~${a['informational_api_equiv']:.3f} (subscription)", style="dim")
        else:
            t.append("cost n/a", style="dim")
        t.append("\n")
    q = u.get("quota")
    if q:
        t.append(f"quota 5h {q['five_hour_pct']:.0f}% used", style="yellow" if q["five_hour_pct"] > 80 else "dim")
    return t


def needs_text(snap: dict) -> Text | None:
    items = snap["attention"]
    if not items:
        return None
    t = Text()
    for i, a in enumerate(items[:3]):
        icon = {"plan_approval": "📋", "task_review": "🔍", "promote": "🚀", "error": "✗", "escalation": "⚠", "question": "?"}.get(a["kind"], "!")
        t.append(f" {icon} ", style="bold yellow")
        t.append(short(a["title"], 110) + "\n", style="bold" if i == 0 else "")
    if len(items) > 3:
        t.append(f"   +{len(items) - 3} more\n", style="dim")
    t.append(" F2 approve · F3 reject · F4 details    (or /approve /reject)", style="dim")
    return t


def readable_stream(cli: str, path: Path, limit: int = 120) -> list[Text]:
    if not path.exists():
        return [Text("(no output yet)", style="dim")]
    data = path.read_bytes()[-120000:].decode(errors="ignore").splitlines()
    out: list[Text] = []
    for line in data:
        for e in streams.parse_line(cli, line):
            k = e["kind"]
            if k == "text" and e.get("text", "").strip():
                out.append(Text("  " + e["text"].strip()[:200]))
            elif k == "tool":
                inp = e.get("input") or {}
                arg = ""
                if isinstance(inp, dict):
                    arg = next((str(inp[x]) for x in ("command", "CommandLine", "file_path", "path", "Path", "TargetFile") if inp.get(x)), "")
                out.append(Text(f"  ⚙ {e.get('name')} {short(arg, 100)}", style="cyan"))
            elif k in ("error", "limit"):
                out.append(Text(f"  ⚠ {e.get('detail', '')[:160]}", style="yellow"))
            elif k == "result":
                out.append(Text("  ✔ finished" if e.get("ok") else f"  ✗ finished with error: {str(e.get('error'))[:120]}", style="green" if e.get("ok") else "red"))
    return out[-limit:] or [Text("(waiting for output)", style="dim")]


# ====================================================================== screens

class TextScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("q", "app.pop_screen", "Back", show=False)]

    def __init__(self, title: str, render_fn):
        super().__init__()
        self.title_, self.render_fn = title, render_fn

    def compose(self) -> ComposeResult:
        yield Static(f" {self.title_}   (esc to go back)", id="screen-title")
        with VerticalScroll():
            yield Static(id="screen-body")
        yield Footer()

    def on_mount(self):
        self.refresh_body()
        self.set_interval(1.0, self.refresh_body)

    def refresh_body(self):
        try:
            self.query_one("#screen-body", Static).update(self.render_fn(self.app.snap))
        except Exception as e:  # never crash the UI on a render bug
            self.query_one("#screen-body", Static).update(Text(f"render error: {e}", style="red"))


def render_usage(snap):
    u = snap["usage"]
    out = []
    def table(title, d):
        tb = Table(title=title, title_justify="left", expand=False, header_style="bold")
        for c in ("", "input", "output", "cache read", "AIU", "API spend", "subscription ≈", "tokens from"):
            tb.add_column(c, justify="right" if c else "left")
        for k, a in sorted(d.items(), key=lambda kv: -kv[1]["total"])[:15]:
            tb.add_row(short(str(k), 22), snapshot._n(a["input"]), snapshot._n(a["output"]), snapshot._n(a["cache_read"]),
                       f"{a['ai_credits']:.3f}" if a["ai_credits"] is not None else "n/a", f"${a['api_spend']:.4f}" if a["api_spend"] else "-",
                       f"~${a['informational_api_equiv']:.4f}" if a["informational_api_equiv"] else "-", a["tokens_known_fraction"])
        return tb
    for title, d in (("Total", {"all": u["global"]}), ("By CLI", u["per_cli"]), ("By model", u["per_model"]), ("By agent", u["per_agent"]),
                     ("By task", {short(next((t["title"] for t in snap["tasks"]["list"] if t["id"] == k), k), 22): v for k, v in u["per_task"].items()}),
                     ("By session", u["per_session"])):
        if d:
            out.append(table(title, d))
    q = u.get("quota")
    from rich.console import Group
    notes = [Text(f"Quota: 5h window {q['five_hour_pct']:.0f}% used  [{q['source']}]" if q else "Quota: not reported by any CLI in use", style="yellow" if q else "dim"),
             Text("API spend counts only CLIs declared billing=api. Subscription usage is shown as an approximate API equivalent, never as spend. AIU is Copilot's own metering unit.", style="dim"),
             Text("n/a = the CLI does not expose that metric.", style="dim")]
    hist = u["history_tokens_by_day"]
    if hist:
        notes.append(Text("Tokens per day: " + "  ".join(f"{d[5:]}: {snapshot._n(v)}" for d, v in list(hist.items())[-7:]), style="dim"))
    return Group(*out, *notes) if out else Group(*notes)


def render_sessions(snap):
    tb = Table(header_style="bold", expand=False)
    for c in ("session", "cli", "model", "agent", "status", "task", "duration", "restarts", "tokens"):
        tb.add_column(c)
    for x in reversed(snap["sessions"]["list"][-60:]):
        tb.add_row(short(x["id"], 20), x["cli"], short(x["model"] or "-", 18), x["agent"] or "-", x["status"], x["task"] or "-", dur(x["duration"]), str(x["restarts"]),
                   snapshot._n(x["tokens"]))
    sv = snap["sessions"]["totals"]
    from rich.console import Group
    return Group(Text(f"{sv['created']} created · {sv['active']} active · {sv['completed']} completed · {sv['failed']} failed · {sv['terminated']} terminated · "
                      f"{snap['sessions']['restarts_total']} restarts", style="bold"), tb)


def render_plan(snap):
    run = snap["run"]
    if not run:
        return Text("No run yet.", style="dim")
    t = Text()
    t.append(f"Objective: {run['prompt']}\n\n", style="bold")
    t.append((run["summary"] or "(no plan yet)") + "\n\n")
    for r in task_rows(snap):
        t.append("  ").append(r[1]).append(" ").append(r[2]).append("  ").append(r[3]).append("\n")
    if run.get("error"):
        t.append(f"\nError: {run['error']}\n", style="red")
    if run.get("final_validation"):
        t.append(f"\nFinal validation: {run['final_validation']}\n")
    return t


def render_agents(snap):
    tb = Table(header_style="bold", expand=False)
    for c in ("#", "agent", "role", "cli", "state", "dur"):
        tb.add_column(c)
    for i, r in enumerate(agent_rows(snap), 1):
        tb.add_row(str(i), r[2], r[3], r[4], r[5], r[6])
    return tb


def render_tasks(snap):
    t = Text()
    for r in task_rows(snap):
        t.append(" ").append(r[1]).append(" ").append(r[2]).append("  ").append(r[3]).append("  ").append(r[4]).append("\n")
    return t if t.plain else Text("no tasks yet", style="dim")


def render_help(snap):
    tb = Table(header_style="bold", expand=False, show_header=False)
    tb.add_column(style="bold cyan"); tb.add_column(style="dim"); tb.add_column()
    for n, a, h in commands.COMMANDS:
        tb.add_row(n, a, h)
    from rich.console import Group
    return Group(Text("Type what you want built and press Enter. Type / for commands.\n"
                      "Keys: Tab cycle panes · Enter on a row opens it · F2 approve · F3 reject · F4 details · Esc back to the input · Ctrl+Q quit (the conductor keeps working)\n", style="bold"), tb)


def render_report(snap):
    return Text(snapshot.render_report(snap))


class AgentScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("ctrl+t", "terminate", "Terminate"), Binding("ctrl+r", "restart", "Restart")]

    def __init__(self, wid: str):
        super().__init__()
        self.wid = wid
        self._n = 0

    def compose(self) -> ComposeResult:
        yield Static(id="agent-head")
        yield RichLog(id="agent-log", wrap=True, markup=False)
        yield Input(placeholder="message this worker (Enter to send)  ·  esc: back  ·  ctrl+t terminate  ·  ctrl+r restart", id="agent-say")
        yield Footer()

    def on_mount(self):
        self.refresh_body()
        self.set_interval(1.0, self.refresh_body)

    def refresh_body(self):
        snap = self.app.snap
        a = next((x for x in snap["agents"] if x["id"] == self.wid), None)
        head = Text()
        if a:
            g, col = STATE_GLYPH.get(a["state"], ("·", "white"))
            head.append(f" {g} ", style=col).append(f"{a['id']}  ", style="bold").append(f"{a['name'] or ''} · {a['cli']}/{a['model'] or 'default'} · {a['state'].lower()} · pid {a['pid'] or '-'} · {dur(a['duration'])} · restarts {a['restarts']}\n")
            if a["task"]:
                head.append(f" task {a['task']}: {next((t['title'] for t in snap['tasks']['list'] if t['id'] == a['task']), '')}\n", style="dim")
            if a["blocker"]:
                head.append(f" ⚠ {a['blocker']}\n", style="yellow")
            u = snap["usage"]["per_session"].get(self.wid)
            if u:
                head.append(f" usage: {snapshot._n(u['input'])} in / {snapshot._n(u['output'])} out" + (f" · {u['ai_credits']:.3f} AIU" if u["ai_credits"] is not None else "") + "\n", style="dim")
        else:
            head.append(f" {self.wid}: not in the live list (finished or ended)", style="dim")
        self.query_one("#agent-head", Static).update(head)
        db = DB(self.app.paths, busy_ms=1000)
        try:
            w = db.one("SELECT run_dir, cli FROM workers WHERE id=?", (self.wid,))
        finally:
            db.close()
        log = self.query_one("#agent-log", RichLog)
        if w:
            lines = readable_stream(w["cli"], Path(w["run_dir"]) / "stream.jsonl")
            if len(lines) != self._n or self._n == 0:
                log.clear()
                for l in lines:
                    log.write(l)
                self._n = len(lines)
        else:
            if self._n == 0:
                log.write(Text("(this is not a worker session: no stream to show)", style="dim")); self._n = 1

    def on_input_submitted(self, ev: Input.Submitted):
        if ev.value.strip():
            self.app.send_command(f"/say {self.wid} {ev.value.strip()}")
            ev.input.value = ""

    def action_terminate(self):
        self.app.send_command(f"/kill {self.wid}")

    def action_restart(self):
        self.app.say("Restart: use /retry <task> to re-queue the task with a fresh worker.", "info")


class TaskScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("f2", "approve", "Approve"), Binding("f3", "reject", "Reject")]

    def __init__(self, tid: str):
        super().__init__()
        self.tid = tid

    def compose(self) -> ComposeResult:
        with Vertical(id="task-modal"):
            yield Static(id="task-body")
            yield Static(" esc: close · F2 approve + merge · F3 send back for rework (REVIEW tasks) ", id="task-hint")

    def on_mount(self):
        db = DB(self.app.paths, busy_ms=1000)
        t = Text()
        try:
            r = tasks_mod.get(db, self.tid)
            t.append(f" {r['title']}\n", style="bold")
            t.append(f" {r['id']} · {r['status']} · owner {r['session_id'] or '-'} · risk {r['risk']} · agent {r['agent'] or '-'}\n\n", style="dim")
            t.append(f" Files: {r['files']}\n Acceptance: {r['acceptance'] or '-'}\n Verify: {r['verify_cmd'] or '(project tests)'}\n")
            if r["handoff"]:
                t.append(f" Handoff: {r['handoff'][:400]}\n")
            v = verify.latest_verification(db, self.tid)
            if v:
                t.append(f"\n Verification: {'PASS' if v['ok'] else 'FAIL'}{' (weak)' if v['weak'] else ''}\n", style="green" if v["ok"] else "red")
                t.append(" " + v["detail"][:900].replace("\n", "\n ") + "\n", style="dim")
            if r["branch"]:
                d = verify.diff(db, self.tid)
                t.append("\n Changes:\n " + d.split("\n\n")[0][:900].replace("\n", "\n ") + "\n")
        except MeshError as e:
            t.append(str(e), style="red")
        finally:
            db.close()
        self.query_one("#task-body", Static).update(t)

    def _item(self):
        return next((a for a in self.app.snap["attention"] if a["kind"] == "task_review" and a["subject"] == self.tid), None)

    def action_approve(self):
        a = self._item()
        if a:
            self.app.enqueue("approve_item", id=a["id"]); self.app.say(f"Approving {self.tid}", "ok"); self.dismiss()
        else:
            self.app.say("This task is not waiting for your approval.", "info")

    def action_reject(self):
        a = self._item()
        if a:
            self.app.enqueue("reject_item", id=a["id"], note="rejected from the task view"); self.app.say(f"Sent {self.tid} back", "warn"); self.dismiss()
        else:
            self.app.say("This task is not waiting for your approval.", "info")


class DetailsScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("f2", "approve", "Approve"), Binding("f3", "reject", "Reject")]

    def __init__(self, item: dict):
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="task-modal"):
            yield Static(Text(f" {self.item['title']}\n\n", style="bold") + Text(self.item["detail"] or "(no further detail)"), id="task-body")
            yield Static(" esc: close · F2 approve · F3 reject ", id="task-hint")

    def action_approve(self):
        self.app.enqueue("approve_item", id=self.item["id"]); self.app.say("Approved.", "ok"); self.dismiss()

    def action_reject(self):
        self.app.enqueue("reject_item", id=self.item["id"], note=""); self.app.say("Rejected.", "warn"); self.dismiss()


class SetupScreen(Screen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, first_run: bool = True):
        super().__init__()
        self.first_run = first_run

    def compose(self) -> ComposeResult:
        paths = self.app.paths
        clis = setup.installed_clis(paths)
        cfg = core.load_config(paths)
        with VerticalScroll(id="setup"):
            yield Static(Text(" Welcome to agent-mesh\n", style="bold magenta") + Text(" Pick the CLI that acts as your orchestrator (it plans the work and answers workers' questions).", style="dim"))
            yield Select([(c, c) for c in clis] or [("(no supported CLI found)", "none")], value=(cfg["app"]["cli"] if cfg["app"]["cli"] in clis else (clis[0] if clis else "none")),
                         allow_blank=False, id="s-cli")
            yield Static(Text("\n How is each CLI billed? (only 'api' counts as spend)", style="dim"))
            for c in clis:
                yield Horizontal(Label(f" {c:<12}"), Select([("subscription", "subscription"), ("api (pay per token)", "api"), ("credits (Copilot AIU)", "credits"), ("unknown", "unknown")],
                                                              value=cfg["billing"].get(c, "unknown") if cfg["billing"].get(c) in ("subscription", "api", "credits", "unknown") else "unknown",
                                                              allow_blank=False, id=f"s-bill-{c}"), classes="setup-row")
            yield Checkbox("Ask me to approve the plan before any work starts (recommended)", value=cfg["autonomy"].get("plan_approval", True), id="s-plan")
            yield Static(Text("\n " + __import__("mesh.cli2", fromlist=["AUTONOMY_TEXT"]).AUTONOMY_TEXT.replace("\n", "\n "), style="yellow"))
            yield Checkbox("I understand and accept this autonomy policy", value=False, id="s-accept")
            yield Button("Save and continue", variant="primary", id="s-save")
            yield Static("", id="s-msg")

    @work(thread=True)
    def _save(self, cli, billing, plan, accepted):
        try:
            setup.apply_setup(self.app.paths, cli, billing, plan, accepted)
            self.app.call_from_thread(self._done)
        except MeshError as e:
            self.app.call_from_thread(lambda: self.query_one("#s-msg", Static).update(Text(f" ✗ {e}", style="red")))
        except Exception as e:
            self.app.call_from_thread(lambda: self.query_one("#s-msg", Static).update(Text(f" ✗ {type(e).__name__}: {e}", style="red")))

    def _done(self):
        self.app.say("Setup saved. Describe what you want built.", "ok")
        self.app.pop_screen()
        self.app.start_conductor()

    def on_button_pressed(self, ev: Button.Pressed):
        if ev.button.id != "s-save":
            return
        cli = self.query_one("#s-cli", Select).value
        billing = {c: self.query_one(f"#s-bill-{c}", Select).value for c in setup.installed_clis(self.app.paths)}
        self.query_one("#s-msg", Static).update(Text(" saving…", style="dim"))
        self._save(cli, billing, self.query_one("#s-plan", Checkbox).value, self.query_one("#s-accept", Checkbox).value)

    def action_cancel(self):
        if not self.first_run:
            self.app.pop_screen()


# ====================================================================== the app

CSS = """
Screen { background: $surface; }
#top { height: 1; background: $panel; padding: 0 0; }
#body { height: 1fr; }
#left { width: 1fr; }
#right { width: 42%; min-width: 60; }
#activity { height: 1fr; border: round $primary-darken-2; padding: 0 1; }
#needs { height: auto; max-height: 7; border: round $warning; padding: 0 0; display: none; }
#agents { height: auto; max-height: 14; min-height: 5; border: round $primary-darken-2; }
#tasks { height: 1fr; border: round $primary-darken-2; }
#usage { height: auto; min-height: 3; max-height: 8; border: round $primary-darken-2; padding: 0 1; }
#prompt { dock: bottom; margin: 0 0; border: round $accent; }
#screen-title { height: 1; background: $panel; }
#task-modal { width: 90%; max-width: 110; height: auto; max-height: 90%; background: $panel; border: round $accent; padding: 1 2; margin: 2 4; }
#task-hint { color: $text-muted; margin-top: 1; }
#agent-head { height: auto; background: $panel; padding: 0 1; }
#agent-log { height: 1fr; border: round $primary-darken-2; }
#setup { padding: 1 4; }
.setup-row { height: 3; }
.setup-row Label { padding-top: 1; }
"""


class MeshApp(App):
    CSS = CSS
    TITLE = "agent-mesh"
    BINDINGS = [
        Binding("ctrl+q", "quit_app", "Quit"),
        Binding("ctrl+c", "ctrl_c", "Quit", show=False),
        Binding("escape", "focus_input", "Input", show=False),
        Binding("f1", "help", "Help"),
        Binding("f2", "approve", "Approve"),
        Binding("f3", "reject", "Reject"),
        Binding("f4", "details", "Details"),
        Binding("f6", "usage", "Usage"),
    ]

    def __init__(self, paths: Paths):
        super().__init__()
        self.paths = paths
        self.snap: dict = {}
        self.feed_keys: set[str] = set()
        self.activity_lines: list[str] = []
        self.top_str = ""
        self.agent_view: list[tuple] = []
        self.task_view: list[tuple] = []
        self.needs_str = ""
        self.usage_str = ""
        self._sig: dict[str, str] = {}
        self._quit_armed = 0.0
        self._echoed: list[str] = []
        self._started_conductor = False
        self.snap = snapshot.build(paths)

    # -- layout
    def compose(self) -> ComposeResult:
        yield Static(id="top")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield RichLog(id="activity", wrap=True, markup=False)
                yield Static(id="needs")
            with Vertical(id="right"):
                yield DataTable(id="agents", cursor_type="row", zebra_stripes=False)
                yield DataTable(id="tasks", cursor_type="row", zebra_stripes=False)
                yield Static(id="usage")
        yield Input(id="prompt", placeholder="Describe what you want built, or type / for commands", suggester=SuggestFromList(commands.NAMES, case_sensitive=False))
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#activity").border_title = "Activity"
        self.query_one("#agents").border_title = "Agents"
        self.query_one("#tasks").border_title = "Tasks"
        self.query_one("#usage").border_title = "Usage"
        self.query_one("#needs").border_title = "Needs you"
        a = self.query_one("#agents", DataTable); a.add_column("", width=1); a.add_column("agent", width=20); a.add_column("cli", width=11); a.add_column("state", width=10); a.add_column("time", width=5)
        t = self.query_one("#tasks", DataTable); t.add_column("", width=1); t.add_column("task", width=27); t.add_column("status", width=12); t.add_column("time", width=6)
        self.query_one("#prompt", Input).focus()
        if not setup.needs_setup(self.paths):
            self.say("Describe what you want built. Type / for commands, F1 for help.", "info")
        self.refresh_data()
        self.set_interval(1.0, self.refresh_data)
        if setup.needs_setup(self.paths):
            self.push_screen(SetupScreen(first_run=True))
        else:
            self.start_conductor()
        self.on_resize()

    def on_resize(self, *_):
        try:
            self.query_one("#right").display = self.size.width >= 100
        except Exception:
            pass

    # -- background helpers
    @work(thread=True, exclusive=False)
    def start_conductor(self) -> None:
        if not self._started_conductor:
            self._started_conductor = True
            try:
                conductor.start_daemon(self.paths)
            except Exception as e:
                self.call_from_thread(self.say, f"Could not start the conductor: {e}", "bad")

    def enqueue(self, kind: str, **payload) -> None:
        db = DB(self.paths)
        try:
            runs.enqueue(db, kind, payload, "human")
        finally:
            db.close()
        self.start_conductor()

    def say(self, text: str, level: str = "info") -> None:
        if not text:
            return
        self.activity_lines.append(text)
        self.query_one("#activity", RichLog).write(Text(f"  {text}", style=LEVEL_STYLE.get(level, "white")))

    # -- data refresh
    def refresh_data(self) -> None:
        try:
            snap = snapshot.build(self.paths)
            items = feed.activity(self.paths)
        except (sqlite3.Error, OSError):
            return
        self.snap = snap
        self.top_str = top_text(snap).plain
        self._set("top", top_text(snap), lambda w, v: w.update(v))
        log = self.query_one("#activity", RichLog)
        for it in items:
            if it["key"] in self.feed_keys:
                continue
            self.feed_keys.add(it["key"])
            if it["kind"] == "you" and it["text"] in self._echoed:
                self._echoed.remove(it["text"])  # already shown immediately when typed
                continue
            line = f"{time.strftime('%H:%M', time.localtime(it['ts']))} {it['icon']} {it['text']}"
            self.activity_lines.append(line)
            style = {"you": "bold cyan", "orchestrator": "bold magenta"}.get(it["kind"], LEVEL_STYLE.get(it["level"], "white"))
            log.write(Text(f" {line}", style=style))
        nt = needs_text(snap)
        self.needs_str = nt.plain if nt else ""
        needs = self.query_one("#needs", Static)
        if nt:
            needs.display = True
            self._set("needs", nt, lambda w, v: w.update(v))
        else:
            needs.display = False
        self.agent_view = agent_rows(snap)
        self.task_view = task_rows(snap)
        self.usage_str = usage_text(snap).plain
        self._set_table("agents", self.agent_view)
        self._set_table("tasks", self.task_view)
        self._set("usage", usage_text(snap), lambda w, v: w.update(v))

    def _set(self, name, value, apply):
        sig = repr(value.plain if isinstance(value, Text) else value)
        if self._sig.get(name) != sig:
            self._sig[name] = sig
            apply(self.query_one(f"#{name}"), value)

    def _set_table(self, name: str, rows: list[tuple]) -> None:
        sig = repr([[c.plain if isinstance(c, Text) else c for c in r] for r in rows])
        if self._sig.get(name) == sig:
            return
        self._sig[name] = sig
        table = self.query_one(f"#{name}", DataTable)
        keep = table.cursor_row
        table.clear()
        for r in rows:
            cells = r[1:] if name == "tasks" else (r[1], r[2], r[4], r[5], r[6])  # agents: the role column is only shown in the full Agents list
            table.add_row(*cells, key=r[0])
        if rows:
            table.move_cursor(row=min(keep, len(rows) - 1))

    # -- input
    def on_input_submitted(self, ev: Input.Submitted) -> None:
        if ev.input.id != "prompt":
            return
        text = ev.value.strip()
        ev.input.value = ""
        if text:
            self.send_command(text)

    @work(thread=True)
    def send_command(self, text: str) -> None:
        if not text.startswith("/"):
            self._echoed.append(text)
            self.call_from_thread(self.say, f"you ▸ {text}", "info")
        reply = commands.execute(self.paths, self.snap, text)
        self.call_from_thread(self.apply_reply, reply)

    def apply_reply(self, r: commands.Reply) -> None:
        if r.kind == "message":
            self.say(r.text, r.level)
        elif r.kind == "quit":
            self.exit()
        elif r.kind == "focus":
            if self.query_one("#right").display:
                self.query_one("#agents").focus()
            else:
                self.open_screen("agents")
        elif r.kind == "screen":
            self.open_screen(r.screen, r.arg)

    def open_screen(self, name: str, arg: str = "") -> None:
        if name == "agent":
            self.push_screen(AgentScreen(arg))
        elif name == "task":
            self.push_screen(TaskScreen(arg))
        elif name == "settings":
            self.push_screen(SetupScreen(first_run=False))
        else:
            fn = {"usage": render_usage, "sessions": render_sessions, "plan": render_plan, "help": render_help, "report": render_report,
                  "agents": render_agents, "tasks": render_tasks}[name]
            self.push_screen(TextScreen({"usage": "Usage", "sessions": "Sessions", "plan": "Plan", "help": "Help", "report": "Report", "agents": "Agents", "tasks": "Tasks"}[name], fn))

    # -- selection / actions
    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        key = ev.row_key.value if ev.row_key else None
        if not key:
            return
        if ev.data_table.id == "agents":
            if any(a["id"] == key and a["kind"] == "worker" for a in self.snap["agents"]):
                self.push_screen(AgentScreen(key))
            else:
                self.say("Only worker agents have a live output view.", "info")
        elif ev.data_table.id == "tasks":
            if not str(key).startswith("T-") or any(t["id"] == key and t["kind"] == "epic" for t in self.snap["tasks"]["list"]):
                return
            self.push_screen(TaskScreen(key))

    def action_focus_input(self) -> None:
        self.query_one("#prompt", Input).focus()

    def action_help(self) -> None:
        self.open_screen("help")

    def action_usage(self) -> None:
        self.open_screen("usage")

    def action_approve(self) -> None:
        self.send_command("/approve")

    def action_reject(self) -> None:
        self.send_command("/reject")

    def action_details(self) -> None:
        if self.snap["attention"]:
            self.push_screen(DetailsScreen(self.snap["attention"][0]))
        else:
            self.say("Nothing is waiting for you.", "info")

    def action_quit_app(self) -> None:
        self.exit()

    def action_ctrl_c(self) -> None:
        if time.time() - self._quit_armed < 2.0:
            self.exit()
        self._quit_armed = time.time()
        self.say("Press Ctrl+C again (or Ctrl+Q) to leave. The conductor keeps working in the background.", "info")
