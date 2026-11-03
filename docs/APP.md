# The `mesh` app (v3)

Run `mesh` in a git repository (first time it offers to set the project up, then a setup wizard asks which CLI is your orchestrator, how each CLI is billed, and for your one-time acceptance of the autonomy policy).

```
 ◆ agent-mesh  my-site   RUN 2 executing  ████░░░░░░ 43%  ● 3 working  ◐ 1 review  conductor ●
╭─ Activity ───────────────────────────╮╭─ Agents ─────────────────────────────╮
│ 14:02 you ▸ Improve the site…        ││ ◆ Orchestrator   claude   idle       │
│ 14:03 ▣ Plan is ready for your review││ ● Luffy model    claude   working 2m │
│ 14:04 ▶ Started frontend on “Luffy…” ││ ◐ Zoro model     copilot  review  4m │
│ 14:09 ✓ “Scaffold” approved          │╰──────────────────────────────────────╯
│ 14:12 ⚠ copilot hit its usage limit  │╭─ Tasks ──────────────────────────────╮
╰──────────────────────────────────────╯│ ▾ Anime-accurate characters          │
╭─ Needs you ──────────────────────────╮│ ✓  Shared cel-shading   completed    │
│ 🔍 'Zoro model' has no checks. OK?   ││ ●  Luffy model          in progress  │
│ F2 approve · F3 reject · F4 details  │╰──────────────────────────────────────╯
╰──────────────────────────────────────╯╭─ Usage ──────────────────────────────╮
╭──────────────────────────────────────╮│ claude  45k tok ~$0.05 (subscription)│
│ Describe what you want built, or /…  ││ copilot 46k tok 0.31 AIU             │
╰──────────────────────────────────────╯╰──────────────────────────────────────╯
```
**Look and feel:** Midnight 80s palette (midnight black `#090909` background, charcoal `#171717` panels, graphite `#262626` raised elements, soft white text, green = success/live, blue = navigation/actions, purple = AI agents, cyan = focus/highlights, amber = warnings, red = errors).

**Sessions pane** lists every live, idle and done session (headless workers, the orchestrator, and any interactive CLI session mesh has seen) except ones you killed or cleared. Select a row: `Enter` opens it, `k` kills it (live, idle or done; a killed worker's unfinished task is parked as blocked so it is not silently restarted, use `/retry`), `c` clears a finished or idle one (never a live one). Bulk: `/clear done`, `/clear idle`, `/clear all`.

**Keys (no function keys):** `Tab` / `Shift+Tab` move input → Needs you → Sessions → Tasks; on *Needs you*: `a` approve, `r` reject, `Enter` details, `n` next; `Esc` returns to the input; `Ctrl+Q` leaves; `?` help (when not typing).

**Usage & limits:** per-CLI tokens/cost. **Antigravity** shows its real limits (verified output of `agy -p /usage`): per model group (Gemini, Claude/GPT) a **5-hour** and a **weekly** window with % left and time to reset. That command costs nothing, so `/limits` fetches it once and turns on a 10-minute refresh (`mesh limits --refresh --out dir` does the same from a shell). **Copilot** does not expose plan limits to headless runs: its `/usage` starts a real model session (~0.7 AIU, verified) and reports session usage only, so it is never automatic (`/limits copilot` runs it on request); mesh instead shows the AIU its own Copilot sessions used. **Claude**'s 5-hour/weekly windows appear when its statusline has reported them. Anything not reported shows as unavailable, never as an estimate.

Type plain text to start a project (or, during a run, to message the orchestrator). Type `/` for commands (`/help`, `/approve`, `/reject`, `/pause`, `/resume`, `/halt`, `/agent`, `/task`, `/retry`, `/say`, `/cli`, `/usage`, `/sessions`, `/kill`, `/clear`, `/limits`, `/promote`, `/report`, `/settings`, `/quit`). Keys: Tab cycles panes, Enter opens the selected agent/task, F2/F3/F4 approve/reject/details, F6 usage, Esc returns to the input, Ctrl+Q leaves.

## How it works
- **The conductor** (a background process, one per project) executes the run: it plans, spawns workers for ready tasks, verifies each submission against the real commit, approves and merges low-risk verified work automatically, sends failures back with feedback, restarts or re-routes failed workers, and runs a final validation. It is plain code, so execution cannot stall because a model ended its turn.
- **The LLM is used twice**: once to plan (structured JSON, validated and repaired once), and as a "brain" that is woken only for judgement calls (a worker's question, an architectural decision, a task that keeps failing). Its answer is JSON actions from a fixed whitelist that the conductor executes. It is shown as the single **Orchestrator** row.
- **The app only reads state and enqueues commands.** Close it any time; reopen `mesh` to re-attach. `/halt` stops all workers; `/stop` stops the conductor.
- **Needs you** lists the only things that require a human: plan approval, high-risk or unverified reviews, repeated failures, and the final promote to your main branch (never automatic).

## Settings (`.mesh/mesh.toml`)
`[conductor] auto_approve = "verified-low-risk" | "ask" | "never"`, `max_rework`, `max_task_attempts`; `[brain] max_turns_per_run`; `[workers]` caps and routing; `[billing]` per CLI; `[autonomy] plan_approval`. Edit freely; `/settings` re-runs the wizard.

## Troubleshooting
`mesh status` (plain text, scriptable) and `mesh doctor` report version skew between the installed tool and the project copy, uncommitted mesh files, stale sessions and the conductor state. Conductor log: `.mesh/runtime/logs/conductor.log`. Old flows still exist for scripting: `mesh start "<prompt>" --wait`, `mesh ctl <command>`, `mesh conduct --status|--stop`, and the v2 subcommands.

## Verify against your real CLIs
`mesh probe <cli> --planner --yes` (planner turn: JSON extracted and valid?), `mesh probe <cli> --yes` (worker stream), `mesh probe <cli> --hooks --yes` (hooks fire in headless mode?).
