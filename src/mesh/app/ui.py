"""The `mesh` app: a Claude-Code-style terminal UI over the conductor, in the Midnight 80s palette.

Left: conversation/activity timeline + a "Needs you" strip. Right: sessions, tasks, usage & limits. Bottom: one input box
(plain text = project prompt / message to the orchestrator; `/` = commands) with a footer of key hints underneath.
No function keys are used. The UI only READS state (snapshot/feed) and ENQUEUES commands for the conductor, so it can be
closed and reopened at any time without affecting execution.
"""
from __future__ import annotations

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
from textual.theme import Theme
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, RichLog, Select, Static

from .. import conductor, core, feed, limits as limits_mod, runs, snapshot, streams, tasks as tasks_mod, verify
from ..db import DB, MeshError, Paths
from . import commands, setup

# ---- Midnight 80s palette
BG, SURFACE, ELEV = "#090909", "#171717", "#262626"
TEXT, MUTED = "#E6E6E6", "#A3A3A3"
GREEN, BLUE, PURPLE, CYAN, AMBER, RED = "#65E572", "#618BFF", "#BD83FF", "#63D6E5", "#E7BD72", "#FF6B6B"

MIDNIGHT = Theme(name="midnight80s", primary=BLUE, secondary=PURPLE, accent=CYAN, warning=AMBER, error=RED, success=GREEN,
                 foreground=TEXT, background=BG, surface=SURFACE, panel=ELEV, dark=True)

STATE_GLYPH = {"WORKING": ("●", GREEN), "COMMUNICATING": ("●", GREEN), "PLANNING": ("●", GREEN), "INITIALISING": ("●", GREEN),
               "REVIEWING": ("◐", AMBER), "THINKING": ("◆", PURPLE), "IDLE": ("○", MUTED), "BLOCKED": ("⏸", AMBER),
               "PAUSED": ("⏸", AMBER), "FAILED": ("✗", RED), "COMPLETED": ("✓", MUTED), "TERMINATED": ("✓", MUTED)}
TASK_GLYPH = {"COMPLETED": ("✓", GREEN), "IN_PROGRESS": ("●", GREEN), "ASSIGNED": ("●", GREEN), "REVIEW": ("◐", AMBER),
              "READY": ("○", TEXT), "BACKLOG": ("○", MUTED), "BLOCKED": ("⏸", AMBER), "PAUSED": ("⏸", AMBER),
              "FAILED": ("✗", RED), "NEEDS_REWORK": ("↺", AMBER), "CANCELLED": ("–", MUTED)}
LEVEL_STYLE = {"info": TEXT, "ok": GREEN, "warn": AMBER, "bad": f"bold {RED}"}
RUN_LABEL = {"planning": "planning", "awaiting_approval": "waiting for you", "executing": "executing", "paused": "paused",
             "integrating": "final checks", "complete": "complete", "failed": "failed", "halted": "halted"}
RUN_STYLE = {"executing": f"bold {GREEN}", "complete": f"bold {GREEN}", "failed": f"bold {RED}", "halted": f"bold {RED}",
             "awaiting_approval": f"bold {AMBER}", "paused": f"bold {AMBER}", "planning": f"bold {BLUE}"}
STATE_LABEL = {"WORKING": "working", "COMMUNICATING": "working", "PLANNING": "starting", "INITIALISING": "starting", "REVIEWING": "in review",
               "THINKING": "thinking", "IDLE": "idle", "BLOCKED": "blocked", "PAUSED": "paused", "FAILED": "failed", "COMPLETED": "done", "TERMINATED": "done"}


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

def top_text(snap: dict) -> Text:
    p, run, cond = snap["project"], snap["run"], snap["conductor"]
    t = Text(" ◆ agent-mesh ", style=f"bold {PURPLE}")
    t.append(f" {p['name']} ", style=f"bold {TEXT}")
    if not run:
        t.append("  no run yet: describe what you want built below", style=MUTED)
    else:
        t.append(f"  RUN {run['id']} ", style=MUTED)
        t.append(RUN_LABEL.get(run["status"], run["status"]), style=RUN_STYLE.get(run["status"], f"bold {TEXT}"))
        if p["leaf_tasks"]:
            t.append(f"  {bar(p['progress'])} {p['progress'] * 100:.0f}%", style=GREEN)
    live = sum(1 for a in snap["agents"] if a.get("bucket") == "live" and a["kind"] != "orchestrator")
    idle = sum(1 for a in snap["agents"] if a.get("bucket") == "idle" and a["kind"] != "orchestrator")
    rev = sum(1 for a in snap["agents"] if a["state"] == "REVIEWING")
    t.append(f"  ● {live} live", style=GREEN)
    if rev:
        t.append(f"  ◐ {rev} review", style=AMBER)
    if idle:
        t.append(f"  ○ {idle} idle", style=MUTED)
    g = snap["usage"]["global"]
    if g.get("api_spend"):
        t.append(f"  API ${g['api_spend']:.2f}", style=CYAN)
    if g.get("ai_credits"):
        t.append(f"  {g['ai_credits']:.2f} AIU", style=CYAN)
    t.append("  conductor ", style=MUTED)
    t.append("●" if cond["running"] else "○ stopped", style=GREEN if cond["running"] else RED)
    return t


def agent_rows(snap: dict) -> list[tuple]:
    """Sessions pane rows: every live / idle / done session that has not been killed or cleared. Order matches `#n` in commands."""
    titles = {t["id"]: t["title"] for t in snap["tasks"]["list"]}
    rows = []
    for a in commands.ordered_agents(snap):
        g, col = STATE_GLYPH.get(a["state"], ("·", TEXT))
        if a["kind"] == "orchestrator":
            name, role = "Orchestrator", ""
            name_style = f"bold {PURPLE}"
        elif a["kind"] == "worker":
            name, role = short(titles.get(a["task"]) or a["name"] or a["id"], 22), (a["name"] or "")
            name_style = TEXT if a.get("bucket") == "live" else MUTED
        else:
            name, role = f"{a['cli']} · {a['id'].split('-')[-1][:6]}", "interactive"
            name_style = TEXT if a.get("bucket") == "live" else MUTED
        label = STATE_LABEL.get(a["state"], a["state"].lower())
        rows.append((a["id"], Text(g, style=col), Text(name, style=name_style), Text(role, style=MUTED), Text(a["cli"], style=MUTED),
                     Text(label, style=col), dur(a["duration"]) if (a["kind"] != "orchestrator" or a["state"] != "IDLE") else ""))
    return rows


def sessions_title(snap: dict) -> str:
    c = {"live": 0, "idle": 0, "failed": 0, "done": 0}
    for a in snap["agents"]:
        if a["kind"] == "orchestrator":
            continue  # the orchestrator is the run's own brain, not a session you manage
        b = a.get("bucket", "live")
        c[b] = c.get(b, 0) + 1
    return f"Sessions   ● {c['live']} live   ○ {c['idle']} idle   ✓ {c['done']} done" + (f"   ✗ {c['failed']} failed" if c["failed"] else "")


def task_rows(snap: dict) -> list[tuple]:
    L = snap["tasks"]["list"]
    by_parent = {}
    for x in L:
        by_parent.setdefault(x["parent"], []).append(x)
    rows = []

    def walk(pid, depth):
        for x in by_parent.get(pid, []):
            if x["kind"] == "epic":
                rows.append((x["id"], Text("▾", style=MUTED), Text(short(x["title"], 28), style=f"bold {TEXT}"), Text(""), Text("")))
            else:
                g, col = TASK_GLYPH.get(x["status"], ("·", TEXT))
                ver = "" if x["verified"] is None else (" ✓" if x["verified"] else " ✗")
                deps = f" ← {len(x['deps'])}" if x["deps"] and x["status"] in ("BACKLOG",) else ""
                rows.append((x["id"], Text(g, style=col), Text("  " * depth + short(x["title"], 26) + deps, style=TEXT if x["status"] not in ("COMPLETED", "CANCELLED") else MUTED),
                             Text(x["status"].lower().replace("_", " "), style=col), Text(dur(x["duration"]) + ver, style=MUTED)))
            walk(x["id"], depth + 1)
    walk(None, 0)
    return rows


def _pct_bar(remaining: float, w: int = 8) -> str:
    return "▕" + bar(remaining / 100.0, w) + "▏"


def limit_lines(snap: dict, clis: list[str]) -> list[Text]:
    """Usage-limit windows exactly as each CLI reports them (Antigravity: per model group, 5-hour and weekly), with honest
    placeholders when a CLI has none to give. Nothing here is estimated."""
    out: list[Text] = []
    lim = snap.get("limits", {})
    order = {"5-hour": 0, "daily": 1, "weekly": 2, "monthly": 3}
    for cli in clis:
        L = lim.get(cli)
        if L is None:
            continue
        if L["windows"]:
            for w in sorted(L["windows"], key=lambda w: (w.get("group") or "", order.get(w["window"], 9))):
                t = Text("  ")
                grp = limits_mod.short_group(w.get("group"))
                if grp:
                    t.append(f"{grp:<11}", style=PURPLE)
                t.append(f"{w['window']:<7}", style=BLUE)
                if w["remaining_pct"] is not None:
                    col = RED if w["remaining_pct"] < 10 else AMBER if w["remaining_pct"] < 30 else GREEN
                    t.append(_pct_bar(w["remaining_pct"]) + f" {w['remaining_pct']:>3.0f}% left", style=col)
                else:
                    t.append(short(w.get("detail") or "reported", 34), style=TEXT)
                r = limits_mod.rel(w.get("resets_ts"))
                if r:
                    t.append(f"  resets in {r}", style=MUTED)
                elif w.get("resets_text"):
                    t.append(f"  resets {w['resets_text']}", style=MUTED)
                out.append(t)
            if L["age"] is not None and L["age"] > 1800:
                out.append(Text(f"  (as of {dur(L['age'])} ago; /limits to refresh)", style=MUTED))
        elif cli == "claude":
            continue  # Claude's windows come from its statusline; nothing to explain when absent
        elif cli == "copilot":
            if L["checked"] is None:
                out.append(Text("  plan limits: not exposed to headless runs", style=MUTED))
            elif L["ok"]:
                out.append(Text("  reports session usage only (no plan limits headlessly)", style=MUTED))
            else:
                out.append(Text(f"  unavailable: {short(L['error'] or 'no answer', 50)}", style=AMBER))
        elif L["checked"] is None:
            out.append(Text("  limits: not fetched · type /limits", style=MUTED))
        elif not L["ok"]:
            out.append(Text(f"  limits unavailable: {short(L['error'] or 'the CLI did not answer', 60)}", style=AMBER))
        else:
            first = next((l.strip() for l in (L["raw"] or "").splitlines() if l.strip()), "")
            out.append(Text(f"  no limit figures reported. Its reply: {short(first, 60)}", style=MUTED))
    return out


def usage_text(snap: dict) -> Text:
    u = snap["usage"]
    t = Text()
    shown = []
    for cli, a in sorted(u["per_cli"].items()):
        tok = (a["input"] or 0) + (a["output"] or 0)
        t.append(f"{cli:<12}", style=f"bold {PURPLE if cli == 'claude' else TEXT}")
        t.append(f"{snapshot._n(tok) if a['tokens_known'] else 'n/a':>7} tok ", style=TEXT)
        if a["ai_credits"] is not None:
            t.append(f"{a['ai_credits']:.2f} AIU", style=CYAN)
        elif a["api_spend"]:
            t.append(f"${a['api_spend']:.3f}", style=CYAN)
        elif a["informational_api_equiv"]:
            t.append(f"~${a['informational_api_equiv']:.3f} (subscription)", style=MUTED)
        else:
            t.append("cost n/a", style=MUTED)
        t.append("\n")
        shown.append(cli)
        for l in limit_lines(snap, [cli]):
            t.append(l).append("\n")
    for cli in ("antigravity", "copilot"):  # these two are the ones with daily/weekly limits to show, even before any usage
        if cli in shown:
            continue
        ll = limit_lines(snap, [cli])
        if ll:
            t.append(f"{cli:<12}", style=f"bold {TEXT}").append("\n")
            for l in ll:
                t.append(l).append("\n")
    if not t.plain.strip():
        return Text("no usage reported yet", style=MUTED)
    t.rstrip()
    return t


def needs_text(snap: dict, focused: bool = False, index: int = 0) -> Text | None:
    items = snap["attention"]
    if not items:
        return None
    t = Text()
    for i, a in enumerate(items[:3]):
        icon = {"plan_approval": "▣", "task_review": "◐", "promote": "⇡", "error": "✗", "escalation": "⚠", "question": "?"}.get(a["kind"], "!")
        sel = "›" if (i == index and len(items) > 1) else " "
        t.append(f"{sel}{icon} ", style=f"bold {AMBER}")
        t.append(short(a["title"], 110) + "\n", style=f"bold {TEXT}" if i == index else TEXT)
    if len(items) > 3:
        t.append(f"   +{len(items) - 3} more\n", style=MUTED)
    t.append(" a approve · r reject · enter details · n next" if focused else " press Tab to focus here, or type /approve  /reject", style=MUTED)
    return t


def readable_stream(cli: str, path: Path, limit: int = 120) -> list[Text]:
    if not path.exists():
        return [Text("(no output yet)", style=MUTED)]
    data = path.read_bytes()[-120000:].decode(errors="ignore").splitlines()
    out: list[Text] = []
    for line in data:
        for e in streams.parse_line(cli, line):
            k = e["kind"]
            if k == "text" and e.get("text", "").strip():
                out.append(Text("  " + e["text"].strip()[:200], style=TEXT))
            elif k == "tool":
                inp = e.get("input") or {}
                arg = ""
                if isinstance(inp, dict):
                    arg = next((str(inp[x]) for x in ("command", "CommandLine", "file_path", "path", "Path", "TargetFile") if inp.get(x)), "")
                out.append(Text(f"  ⚙ {e.get('name')} {short(arg, 100)}", style=CYAN))
            elif k in ("error", "limit"):
                out.append(Text(f"  ⚠ {e.get('detail', '')[:160]}", style=AMBER))
            elif k == "result":
                out.append(Text("  ✔ finished" if e.get("ok") else f"  ✗ finished with error: {str(e.get('error'))[:120]}", style=GREEN if e.get("ok") else RED))
    return out[-limit:] or [Text("(waiting for output)", style=MUTED)]


# ====================================================================== text screens

class TextScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("q", "app.pop_screen", "Back", show=False)]

    def __init__(self, title: str, render_fn):
        super().__init__()
        self.title_, self.render_fn = title, render_fn

    def compose(self) -> ComposeResult:
        yield Static(f" {self.title_}   ·   esc to go back", id="screen-title")
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
            self.query_one("#screen-body", Static).update(Text(f"render error: {e}", style=RED))


def _table(title=None, cols=()):
    tb = Table(title=title, title_justify="left", expand=False, header_style=f"bold {BLUE}", border_style=ELEV)
    left = ("session", "cli", "model", "agent", "status", "task", "role", "state")
    for c in cols:
        tb.add_column(c, justify="left" if (not c or c in left) else "right")
    return tb


def render_usage(snap):
    from rich.console import Group
    u = snap["usage"]
    out = []

    def table(title, d):
        tb = _table(title, ("", "input", "output", "cache read", "AIU", "API spend", "subscription ≈", "tokens from"))
        for k, a in sorted(d.items(), key=lambda kv: -kv[1]["total"])[:15]:
            tb.add_row(short(str(k), 22), snapshot._n(a["input"]), snapshot._n(a["output"]), snapshot._n(a["cache_read"]),
                       f"{a['ai_credits']:.3f}" if a["ai_credits"] is not None else "n/a", f"${a['api_spend']:.4f}" if a["api_spend"] else "-",
                       f"~${a['informational_api_equiv']:.4f}" if a["informational_api_equiv"] else "-", a["tokens_known_fraction"])
        return tb

    lt = Text("Usage limits (as reported by each CLI)\n", style=f"bold {BLUE}")
    for cli in ("antigravity", "copilot", "claude"):
        L = snap.get("limits", {}).get(cli)
        if not L:
            continue
        lt.append(f"\n{cli}", style=f"bold {PURPLE if cli == 'claude' else TEXT}")
        lt.append(f"   source: {L['command'] or 'n/a'}" + (f"   checked {dur(L['age'])} ago" if L["age"] is not None else "") + "\n", style=MUTED)
        ls = limit_lines(snap, [cli])
        for l in ls:
            lt.append(l).append("\n")
        if cli == "claude" and not L["windows"]:
            lt.append("  no 5-hour / weekly figures yet (Claude reports them to interactive sessions' statusline)\n", style=MUTED)
        if cli == "copilot":
            lt.append("  Copilot exposes plan limits only in its interactive footer. mesh shows the AIU its own sessions used (see below);\n"
                      "  `/limits copilot` runs its /usage, which starts a real model session (~0.7 AIU) and reports session usage only.\n", style=MUTED)
        if L["raw"] and not L["windows"] and cli != "copilot":
            lt.append("  CLI output:\n", style=MUTED)
            for ln in L["raw"].splitlines()[:14]:
                lt.append(f"    {ln[:110]}\n", style=MUTED)
    out.append(lt)
    for title, d in (("Total", {"all": u["global"]}), ("By CLI", u["per_cli"]), ("By model", u["per_model"]), ("By agent", u["per_agent"]),
                     ("By task", {short(next((t["title"] for t in snap["tasks"]["list"] if t["id"] == k), k), 22): v for k, v in u["per_task"].items()}),
                     ("By session", u["per_session"])):
        if d:
            out.append(table(title, d))
    notes = [Text("API spend counts only CLIs declared billing=api. Subscription usage is shown as an approximate API equivalent, never as spend. "
                  "AIU is Copilot's own metering unit. n/a = the CLI does not expose that metric. Limits are never estimated.", style=MUTED)]
    hist = u["history_tokens_by_day"]
    if hist:
        notes.append(Text("Tokens per day: " + "  ".join(f"{d[5:]}: {snapshot._n(v)}" for d, v in list(hist.items())[-7:]), style=MUTED))
    return Group(*out, *notes)


def render_sessions(snap):
    from rich.console import Group
    tb = _table(None, ("session", "cli", "model", "agent", "status", "task", "duration", "restarts", "tokens"))
    for x in reversed(snap["sessions"]["list"][-60:]):
        tb.add_row(short(x["id"], 20), x["cli"], short(x["model"] or "-", 18), x["agent"] or "-", x["status"], x["task"] or "-", dur(x["duration"]), str(x["restarts"]),
                   snapshot._n(x["tokens"]))
    sv = snap["sessions"]["totals"]
    return Group(Text(f"{sv['created']} created · {sv['active']} active · {sv['completed']} completed · {sv['failed']} failed · {sv['terminated']} terminated · "
                      f"{snap['sessions']['restarts_total']} restarts   (killed and cleared sessions are not listed)", style=f"bold {TEXT}"), tb)


def render_plan(snap):
    run = snap["run"]
    if not run:
        return Text("No run yet.", style=MUTED)
    t = Text()
    t.append(f"Objective: {run['prompt']}\n\n", style=f"bold {TEXT}")
    t.append((run["summary"] or "(no plan yet)") + "\n\n", style=TEXT)
    for r in task_rows(snap):
        t.append("  ").append(r[1]).append(" ").append(r[2]).append("  ").append(r[3]).append("\n")
    if run.get("error"):
        t.append(f"\nError: {run['error']}\n", style=RED)
    if run.get("final_validation"):
        t.append(f"\nFinal validation: {run['final_validation']}\n", style=TEXT)
    return t


def render_agents(snap):
    tb = _table(None, ("#", "session", "role", "cli", "state", "time"))
    for i, r in enumerate(agent_rows(snap), 1):
        tb.add_row(str(i), r[2], r[3], r[4], r[5], r[6])
    return tb


def render_tasks(snap):
    t = Text()
    for r in task_rows(snap):
        t.append(" ").append(r[1]).append(" ").append(r[2]).append("  ").append(r[3]).append("  ").append(r[4]).append("\n")
    return t if t.plain else Text("no tasks yet", style=MUTED)


def render_help(snap):
    from rich.console import Group
    tb = Table(expand=False, show_header=False, border_style=ELEV)
    tb.add_column(style=f"bold {CYAN}"); tb.add_column(style=MUTED); tb.add_column(style=TEXT)
    for n, a, h in commands.COMMANDS:
        tb.add_row(n, a, h)
    return Group(Text("Type what you want built and press Enter. Type / for commands.\n"
                      "Keys: Tab / Shift+Tab move between the input, Needs-you, Sessions and Tasks · Enter opens the selected row · "
                      "on Sessions: k kill, c clear · on Needs-you: a approve, r reject, Enter details, n next · Esc returns to the input · "
                      "Ctrl+Q quits (the conductor keeps working). No function keys are used.\n", style=f"bold {TEXT}"), tb)


def render_report(snap):
    return Text(snapshot.render_report(snap), style=TEXT)


# ====================================================================== screens with actions

class AgentScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("ctrl+t", "kill", "Kill session")]

    def __init__(self, wid: str):
        super().__init__()
        self.wid = wid
        self._n = 0

    def compose(self) -> ComposeResult:
        yield Static(id="agent-head")
        yield RichLog(id="agent-log", wrap=True, markup=False)
        yield Input(placeholder="message this worker and press Enter   ·   esc back   ·   ctrl+t kill this session", id="agent-say")
        yield Footer()

    def on_mount(self):
        self.refresh_body()
        self.set_interval(1.0, self.refresh_body)

    def refresh_body(self):
        snap = self.app.snap
        a = next((x for x in snap["agents"] if x["id"] == self.wid), None)
        head = Text()
        if a:
            g, col = STATE_GLYPH.get(a["state"], ("·", TEXT))
            head.append(f" {g} ", style=col).append(f"{a['id']}  ", style=f"bold {TEXT}")
            head.append(f"{a['name'] or ''} · {a['cli']}/{a['model'] or 'default'} · {STATE_LABEL.get(a['state'], a['state'].lower())} · pid {a['pid'] or '-'} · {dur(a['duration'])} · restarts {a['restarts']}\n", style=TEXT)
            if a["task"]:
                head.append(f" task {a['task']}: {next((t['title'] for t in snap['tasks']['list'] if t['id'] == a['task']), '')}\n", style=MUTED)
            if a["blocker"]:
                head.append(f" ⚠ {a['blocker']}\n", style=AMBER)
            u = snap["usage"]["per_session"].get(self.wid)
            if u:
                head.append(f" usage: {snapshot._n(u['input'])} in / {snapshot._n(u['output'])} out" + (f" · {u['ai_credits']:.3f} AIU" if u["ai_credits"] is not None else "") + "\n", style=MUTED)
        else:
            head.append(f" {self.wid}: not in the list (killed or cleared)", style=MUTED)
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
        elif self._n == 0:
            log.write(Text("(this is not a headless worker: there is no stream to show)", style=MUTED)); self._n = 1

    def on_input_submitted(self, ev: Input.Submitted):
        if ev.value.strip():
            self.app.send_command(f"/say {self.wid} {ev.value.strip()}")
            ev.input.value = ""

    def action_kill(self):
        self.app.send_command(f"/kill {self.wid}")
        self.app.pop_screen()


class TaskScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("a", "approve", "Approve + merge"), Binding("r", "reject", "Send back")]

    def __init__(self, tid: str):
        super().__init__()
        self.tid = tid

    def compose(self) -> ComposeResult:
        with Vertical(id="task-modal"):
            yield Static(id="task-body")
            yield Static(" esc close · a approve + merge · r send back for rework   (only for tasks waiting on you) ", id="task-hint")

    def on_mount(self):
        db = DB(self.app.paths, busy_ms=1000)
        t = Text()
        try:
            r = tasks_mod.get(db, self.tid)
            t.append(f" {r['title']}\n", style=f"bold {TEXT}")
            t.append(f" {r['id']} · {r['status']} · owner {r['session_id'] or '-'} · risk {r['risk']} · agent {r['agent'] or '-'}\n\n", style=MUTED)
            t.append(f" Files: {r['files']}\n Acceptance: {r['acceptance'] or '-'}\n Verify: {r['verify_cmd'] or '(project tests)'}\n", style=TEXT)
            if r["handoff"]:
                t.append(f" Handoff: {r['handoff'][:400]}\n", style=TEXT)
            v = verify.latest_verification(db, self.tid)
            if v:
                t.append(f"\n Verification: {'PASS' if v['ok'] else 'FAIL'}{' (weak)' if v['weak'] else ''}\n", style=GREEN if v["ok"] else RED)
                t.append(" " + v["detail"][:900].replace("\n", "\n ") + "\n", style=MUTED)
            if r["branch"]:
                d = verify.diff(db, self.tid)
                t.append("\n Changes:\n " + d.split("\n\n")[0][:900].replace("\n", "\n ") + "\n", style=TEXT)
        except MeshError as e:
            t.append(str(e), style=RED)
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
    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("a", "approve", "Approve"), Binding("r", "reject", "Reject")]

    def __init__(self, item: dict):
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="task-modal"):
            yield Static(Text(f" {self.item['title']}\n\n", style=f"bold {TEXT}") + Text(self.item["detail"] or "(no further detail)", style=TEXT), id="task-body")
            yield Static(" esc close · a approve · r reject ", id="task-hint")

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
        from .. import cli2
        paths = self.app.paths
        clis = setup.installed_clis(paths)
        cfg = core.load_config(paths)
        with VerticalScroll(id="setup"):
            yield Static(Text(" Welcome to agent-mesh\n", style=f"bold {PURPLE}") + Text(" Pick the CLI that acts as your orchestrator (it plans the work and answers workers' questions).", style=MUTED))
            yield Select([(c, c) for c in clis] or [("(no supported CLI found)", "none")], value=(cfg["app"]["cli"] if cfg["app"]["cli"] in clis else (clis[0] if clis else "none")),
                         allow_blank=False, id="s-cli")
            yield Static(Text("\n How is each CLI billed? (only 'api' counts as spend)", style=MUTED))
            for c in clis:
                yield Horizontal(Label(f" {c:<12}"), Select([("subscription", "subscription"), ("api (pay per token)", "api"), ("credits (Copilot AIU)", "credits"), ("unknown", "unknown")],
                                                              value=cfg["billing"].get(c, "unknown") if cfg["billing"].get(c) in ("subscription", "api", "credits", "unknown") else "unknown",
                                                              allow_blank=False, id=f"s-bill-{c}"), classes="setup-row")
            yield Checkbox("Ask me to approve the plan before any work starts (recommended)", value=cfg["autonomy"].get("plan_approval", True), id="s-plan")
            yield Static(Text("\n " + cli2.AUTONOMY_TEXT.replace("\n", "\n "), style=AMBER))
            yield Checkbox("I understand and accept this autonomy policy", value=False, id="s-accept")
            yield Button("Save and continue", variant="primary", id="s-save")
            yield Static("", id="s-msg")

    @work(thread=True)
    def _save(self, cli, billing, plan, accepted):
        try:
            setup.apply_setup(self.app.paths, cli, billing, plan, accepted)
            self.app.call_from_thread(self._done)
        except MeshError as e:
            self.app.call_from_thread(lambda: self.query_one("#s-msg", Static).update(Text(f" ✗ {e}", style=RED)))
        except Exception as e:
            self.app.call_from_thread(lambda: self.query_one("#s-msg", Static).update(Text(f" ✗ {type(e).__name__}: {e}", style=RED)))

    def _done(self):
        self.app.say("Setup saved. Describe what you want built.", "ok")
        self.app.pop_screen()
        self.app.start_conductor()

    def on_button_pressed(self, ev: Button.Pressed):
        if ev.button.id != "s-save":
            return
        cli = self.query_one("#s-cli", Select).value
        billing = {c: self.query_one(f"#s-bill-{c}", Select).value for c in setup.installed_clis(self.app.paths)}
        self.query_one("#s-msg", Static).update(Text(" saving…", style=MUTED))
        self._save(cli, billing, self.query_one("#s-plan", Checkbox).value, self.query_one("#s-accept", Checkbox).value)

    def action_cancel(self):
        if not self.first_run:
            self.app.pop_screen()


# ====================================================================== widgets with their own key hints (shown in the footer when focused)

class SessionTable(DataTable):
    BINDINGS = [Binding("enter", "select_cursor", "Open"), Binding("k", "kill", "Kill"), Binding("c", "clear", "Clear")]

    def _sid(self):
        try:
            return self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value
        except Exception:
            return None

    def action_kill(self):
        sid = self._sid()
        if sid:
            self.app.send_command(f"/kill {sid}")

    def action_clear(self):
        sid = self._sid()
        if sid:
            self.app.send_command(f"/clear {sid}")


class TaskTable(DataTable):
    BINDINGS = [Binding("enter", "select_cursor", "Open")]


class NeedsYou(Static, can_focus=True):
    BINDINGS = [Binding("a", "approve", "Approve"), Binding("r", "reject", "Reject"), Binding("enter", "details", "Details"),
                Binding("n", "next_item", "Next")]

    def action_approve(self):
        self.app.action_approve()

    def action_reject(self):
        self.app.action_reject()

    def action_details(self):
        self.app.action_details()

    def action_next_item(self):
        self.app.rotate_attention()


# ====================================================================== the app

CSS = f"""
Screen {{ background: {BG}; color: {TEXT}; }}
#top {{ height: 1; background: {ELEV}; color: {TEXT}; }}
#body {{ height: 1fr; }}
#left {{ width: 1fr; }}
#right {{ width: 42%; min-width: 60; }}
#activity {{ height: 1fr; background: {SURFACE}; border: round {ELEV}; border-title-color: {BLUE}; padding: 0 1; }}
#needs {{ dock: bottom; height: auto; max-height: 8; background: {SURFACE}; border: round {AMBER}; border-title-color: {AMBER}; }}
#needs:focus {{ border: round {CYAN}; }}
#agents {{ height: 1fr; min-height: 6; background: {SURFACE}; border: round {ELEV}; border-title-color: {BLUE}; border-subtitle-color: {MUTED}; }}
#agents:focus {{ border: round {CYAN}; }}
#tasks {{ height: 1fr; min-height: 6; background: {SURFACE}; border: round {ELEV}; border-title-color: {BLUE}; }}
#tasks:focus {{ border: round {CYAN}; }}
#usage {{ height: auto; min-height: 3; max-height: 14; background: {SURFACE}; border: round {ELEV}; border-title-color: {BLUE}; padding: 0 1; }}
DataTable {{ background: {SURFACE}; color: {TEXT}; }}
DataTable > .datatable--header {{ background: {ELEV}; color: {MUTED}; text-style: bold; }}
DataTable > .datatable--cursor {{ background: {ELEV}; color: {CYAN}; text-style: bold; }}
DataTable > .datatable--hover {{ background: {ELEV}; }}
DataTable:focus > .datatable--cursor {{ background: {ELEV}; color: {CYAN}; text-style: bold; }}
#prompt {{ height: 3; background: {SURFACE}; color: {TEXT}; border: round {BLUE}; margin: 0; }}
#prompt:focus {{ border: round {CYAN}; }}
Input > .input--placeholder {{ color: {MUTED}; }}
Input > .input--cursor {{ background: {CYAN}; color: {BG}; }}
Input > .input--suggestion {{ color: {MUTED}; }}
* {{ scrollbar-background: {SURFACE}; scrollbar-background-hover: {ELEV}; scrollbar-background-active: {ELEV};
     scrollbar-color: {ELEV}; scrollbar-color-hover: {MUTED}; scrollbar-color-active: {CYAN}; scrollbar-corner-color: {SURFACE}; }}
Footer {{ background: {SURFACE}; color: {MUTED}; }}
FooterKey {{ background: {SURFACE}; }}
FooterKey .footer-key--key {{ color: {CYAN}; background: {SURFACE}; text-style: bold; }}
FooterKey .footer-key--description {{ color: {MUTED}; background: {SURFACE}; }}
#screen-title {{ height: 1; background: {ELEV}; color: {TEXT}; }}
#task-modal {{ width: 90%; max-width: 110; height: auto; max-height: 90%; background: {SURFACE}; border: round {BLUE}; padding: 1 2; margin: 2 4; }}
#task-hint {{ color: {MUTED}; margin-top: 1; }}
#agent-head {{ height: auto; background: {ELEV}; padding: 0 1; }}
#agent-log {{ height: 1fr; background: {SURFACE}; border: round {ELEV}; }}
#setup {{ padding: 1 4; }}
.setup-row {{ height: 3; }}
.setup-row Label {{ padding-top: 1; color: {TEXT}; }}
Button.-primary {{ background: {BLUE}; color: {BG}; }}
Select, Checkbox {{ background: {SURFACE}; color: {TEXT}; }}
"""


class MeshApp(App):
    CSS = CSS
    TITLE = "agent-mesh"
    ENABLE_COMMAND_PALETTE = False  # the app has its own / commands
    BINDINGS = [
        Binding("ctrl+q", "quit_app", "Quit"),
        Binding("ctrl+c", "ctrl_c", "Quit", show=False),
        Binding("escape", "focus_input", "Type", show=False),
        Binding("question_mark", "help", "Help", show=False),
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
        self.sessions_title_str = ""
        self._sig: dict[str, str] = {}
        self._quit_armed = 0.0
        self._started_conductor = False
        self._echoed: list[str] = []
        self._revived = 0.0
        self._att_index = 0
        self._cmd_seen = self._max_command_id()
        self.snap = snapshot.build(paths)

    def _max_command_id(self) -> int:
        try:
            db = DB(self.paths, busy_ms=1000)
            try:
                return db.one("SELECT COALESCE(MAX(id),0) FROM commands")[0]
            finally:
                db.close()
        except Exception:
            return 0

    # -- layout
    def compose(self) -> ComposeResult:
        yield Static(id="top")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield NeedsYou(id="needs")  # first in focus order (Tab from the input lands here); docked to the bottom of the pane
                yield RichLog(id="activity", wrap=True, markup=False)
            with Vertical(id="right"):
                yield SessionTable(id="agents", cursor_type="row", zebra_stripes=False)
                yield TaskTable(id="tasks", cursor_type="row", zebra_stripes=False)
                yield Static(id="usage")
        yield Input(id="prompt", placeholder="Describe what you want built, or type / for commands", suggester=SuggestFromList(commands.NAMES, case_sensitive=False))
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(MIDNIGHT)
        self.theme = "midnight80s"
        self.query_one("#activity").border_title = "Activity"
        self.query_one("#activity").can_focus = False  # a feed, scrolled with the mouse wheel; keeps Tab: input -> Needs you -> Sessions -> Tasks
        self.query_one("#agents").border_title = "Sessions"
        self.query_one("#agents").border_subtitle = "enter open · k kill · c clear"
        self.query_one("#tasks").border_title = "Tasks"
        self.query_one("#usage").border_title = "Usage & limits"
        self.query_one("#needs").border_title = "Needs you"
        a = self.query_one("#agents", DataTable); a.add_column("", width=1); a.add_column("session", width=22); a.add_column("cli", width=11); a.add_column("state", width=9); a.add_column("time", width=5)
        t = self.query_one("#tasks", DataTable); t.add_column("", width=1); t.add_column("task", width=27); t.add_column("status", width=12); t.add_column("time", width=6)
        self.query_one("#prompt", Input).focus()
        if not setup.needs_setup(self.paths):
            self.say("Describe what you want built. Type / for commands. Tab moves between panes.", "info")
        self.refresh_data()
        self.set_interval(1.0, self.refresh_data)
        if setup.needs_setup(self.paths):
            self.push_screen(SetupScreen(first_run=True))
        else:
            self.start_conductor()
        self.on_resize()

    def on_resize(self, event=None):
        width = getattr(getattr(event, "size", None), "width", None) or self.size.width
        self._apply_width(width)
        # a quick shrink-then-grow can deliver the event before the final size is settled: re-check shortly after
        self.set_timer(0.3, lambda: self._apply_width(self.size.width))

    def _apply_width(self, width: int) -> None:
        try:
            right = self.query_one("#right")
            want = width >= 100
            if right.display != want:
                right.display = want
                self.refresh(layout=True)
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
        if self.activity_lines and self.activity_lines[-1] == text:
            return  # never print the same line twice in a row (e.g. pressing Enter repeatedly on a row)
        self.activity_lines.append(text)
        self.query_one("#activity", RichLog).write(Text(f"  {text}", style=LEVEL_STYLE.get(level, TEXT)))

    # -- data refresh
    def refresh_data(self) -> None:
        try:
            snap = snapshot.build(self.paths)
            items = feed.activity(self.paths)
            results = feed.command_results(self.paths, self._cmd_seen)
        except (sqlite3.Error, OSError):
            return
        self.snap = snap
        if snap.get("pending_commands") and not snap["conductor"]["running"] and time.time() - self._revived > 10:
            self._revived = time.time()
            self._started_conductor = False
            self.start_conductor()  # commands are waiting but nothing is processing them (it exited or was stopped): bring it back
            self.say("The conductor was not running; restarting it.", "warn")
        tt = top_text(snap)
        self.top_str = tt.plain
        self._set("top", tt, lambda w, v: w.update(v))
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
            style = {"you": f"bold {BLUE}", "orchestrator": f"bold {PURPLE}"}.get(it["kind"], LEVEL_STYLE.get(it["level"], TEXT))
            log.write(Text(f" {line}", style=style))
        for r in results:
            self._cmd_seen = max(self._cmd_seen, r["id"])
            if r["text"]:
                self.say(r["text"], "bad" if r["status"] == "error" else "ok")
        self._att_index = min(self._att_index, max(0, len(snap["attention"]) - 1))
        needs = self.query_one("#needs", NeedsYou)
        nt = needs_text(snap, focused=needs.has_focus, index=self._att_index)
        self.needs_str = nt.plain if nt else ""
        if nt:
            needs.display = True
            self._set("needs", nt, lambda w, v: w.update(v))
        else:
            needs.display = False
        self.agent_view = agent_rows(snap)
        self.task_view = task_rows(snap)
        ut = usage_text(snap)
        self.usage_str = ut.plain
        self.sessions_title_str = sessions_title(snap)
        self.query_one("#agents").border_title = self.sessions_title_str
        self._set_table("agents", self.agent_view)
        self._set_table("tasks", self.task_view)
        self._set("usage", ut, lambda w, v: w.update(v))

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
            cells = r[1:] if name == "tasks" else (r[1], r[2], r[4], r[5], r[6])  # sessions: role only appears in the full /agents list
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
            self.push_screen(TextScreen({"usage": "Usage & limits", "sessions": "Session history", "plan": "Plan", "help": "Help", "report": "Report", "agents": "Sessions", "tasks": "Tasks"}[name], fn))

    # -- selection / actions
    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        key = ev.row_key.value if ev.row_key else None
        if not key:
            return
        if ev.data_table.id == "agents":
            if any(a["id"] == key and a["kind"] == "worker" for a in self.snap["agents"]):
                self.push_screen(AgentScreen(key))
            else:
                self.say("No live output for this session (only headless workers have one). k kills it, c clears it.", "info")
        elif ev.data_table.id == "tasks":
            if not str(key).startswith("T-") or any(t["id"] == key and t["kind"] == "epic" for t in self.snap["tasks"]["list"]):
                return
            self.push_screen(TaskScreen(key))

    def on_descendant_focus(self, *_):
        self._sig.pop("needs", None)  # re-render the strip so its hint reflects focus

    def action_focus_input(self) -> None:
        self.query_one("#prompt", Input).focus()

    def action_help(self) -> None:
        self.open_screen("help")

    def rotate_attention(self) -> None:
        n = len(self.snap["attention"])
        if n:
            self._att_index = (self._att_index + 1) % n
            self._sig.pop("needs", None)
            self.refresh_data()

    def _current_item(self):
        a = self.snap["attention"]
        return a[min(self._att_index, len(a) - 1)] if a else None

    def action_approve(self) -> None:
        it = self._current_item()
        if not it:
            self.say("Nothing is waiting for you.", "info")
            return
        self.enqueue("approve_item", id=it["id"])
        self.say(f"Approving: {it['title'][:90]}", "ok")

    def action_reject(self) -> None:
        it = self._current_item()
        if not it:
            self.say("Nothing is waiting for you.", "info")
            return
        self.enqueue("reject_item", id=it["id"], note="")
        self.say(f"Rejecting: {it['title'][:90]}", "warn")

    def action_details(self) -> None:
        it = self._current_item()
        if it:
            self.push_screen(DetailsScreen(it))
        else:
            self.say("Nothing is waiting for you.", "info")

    def action_quit_app(self) -> None:
        self.exit()

    def action_ctrl_c(self) -> None:
        if time.time() - self._quit_armed < 2.0:
            self.exit()
        self._quit_armed = time.time()
        self.say("Press Ctrl+C again (or Ctrl+Q) to leave. The conductor keeps working in the background.", "info")
