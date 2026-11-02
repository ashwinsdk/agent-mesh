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
Type plain text to start a project (or, during a run, to message the orchestrator). Type `/` for commands (`/help`, `/approve`, `/reject`, `/pause`, `/resume`, `/halt`, `/agent`, `/task`, `/retry`, `/say`, `/cli`, `/usage`, `/sessions`, `/promote`, `/report`, `/settings`, `/quit`). Keys: Tab cycles panes, Enter opens the selected agent/task, F2/F3/F4 approve/reject/details, F6 usage, Esc returns to the input, Ctrl+Q leaves.

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
