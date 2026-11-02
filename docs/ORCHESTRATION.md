# Orchestration (v2)

One prompt in, a supervised team out. You talk to one CLI; it plans, spawns real worker sessions, supervises, verifies and integrates.

## Concepts (kept distinct everywhere, including the dashboard)
| | What | Lifetime |
|---|---|---|
| **Agent** | a role definition (`.mesh/agents/*.md`: backend, qa, …) | static |
| **Session** | a resumable CLI conversation (native id) with a CLI + model | survives process death |
| **Process** | the OS process (runner + CLI child) currently hosting it | ephemeral |
| **Worker** | binds agent + session + current process + task assignment; has a lifecycle state and restart count | one task |

## Start
```sh
mesh up claude "Build a modern 3D website for a premium water bottle brand with a backend"   # or copilot / antigravity
mesh dash            # another terminal: tabs Overview · Agents · Sessions · Tasks · Comms · Usage   (mesh dash --web for a read-only browser view)
mesh up claude --resume   # re-attach after closing the orchestrator; workers keep running
mesh halt            # emergency stop: terminates every worker process group and blocks new spawns (mesh unhalt)
```
First run shows the **autonomy policy** and requires you to accept it (recorded in `.mesh/mesh.toml [autonomy]`). It is needed because headless workers cannot answer permission prompts. `mesh up` also commits the mesh-generated config on your base branch (workers branch from it, so they inherit the safety hooks).

## What the orchestrator does (shipped playbook, `src/mesh/playbooks.py`, you never edit it)
1. **Plan**: facts/ADRs, epic + tasks with `--files` ownership, `--dep`, `--accept`, `--verify "<cmd>"`, `--risk`. Shows `mesh plan show`; with `plan_approval = true` (default) it waits for your go-ahead.
2. **Delegate**: `mesh worker spawn --task T [--agent --cli --model]`. Routing per role in `[workers.route]` (e.g. `backend = ["claude:opus","copilot"]`), first available CLI wins, the rest are the fallback chain.
3. **Supervise**: parks in `mesh wait` (blocks cheaply; wakes in <1 s on a relevant event) and is also given an event digest at its own tool boundaries by hooks.
4. **Verify**: `mesh verify T` (ownership check + task verify command + project tests, bound to the commit) → `mesh approve T` / `mesh reject T --reason`. Workers can only `mesh task submit`; they cannot approve, integrate, spawn, or halt (enforced by session role).
5. **Integrate/finish**: `mesh integrate T` → `mesh/integration`; the human promotes with `mesh promote`.

## Worker lifecycle & recovery
`INITIALISING → PLANNING → WORKING ⇄ COMMUNICATING → REVIEWING → COMPLETED` (also IDLE, BLOCKED, PAUSED, FAILED, TERMINATED). PLANNING/COMMUNICATING/REVIEWING are inferred from stream/tool events (heuristic).
Each worker has a detached **runner** process (`mesh worker run`, own process group). It parses the CLI's stream, heartbeats, keeps file locks alive, and flags stalls (`stall_seconds`) and timeouts. Exit policy:
- clean exit + task submitted → COMPLETED; clean exit **without** submitting → FAILED + task released + blocker message (never counted as done)
- crash / kill -9 / reboot → `reconcile` (run by `mesh worker list`, `mesh wait`, `mesh doctor`) detects the dead runner, checkpoints, restarts (resume the same native session where the CLI supports it, else fresh session + checkpoint brief), up to `max_restarts`
- usage/rate limit → checkpoint, switch to the next CLI in the route (`worker.fallback`), else FAILED and task released to the orchestrator
- after `max_restarts` → FAILED, task returned to READY, orchestrator notified

## Communication
Durable messages with `correlation_id`, `reply_to`, `requires_ack`, `expires_at`, `attempts`, `delivery_state`. Delivery to a running worker/orchestrator happens at its next tool boundary (Claude/Copilot `additionalContext`, Antigravity `PreInvocation injectSteps`); otherwise via the resume brief. `bus.sweep` retries unacknowledged messages, then marks them failed (`msg.delivery_failed`). On approval, dependents receive `dependency_ready` with the producer's handoff, and their spawn prompt includes it. Workers' architectural proposals are `decision` messages: only the orchestrator ratifies them (`mesh decide`) into ADRs, which are included in every later worker prompt. Workers may request help with `mesh worker request`; the orchestrator approves (`approve-spawn`), subject to `max_depth`.

## Guardrails
worktree-confined workers; hooks (destructive/secret/lock/protected-file denials); ownership check at verify time; allow-listed worker environment (no ambient secrets); `max_workers`, `max_depth`, `max_restarts`, per-worker budget flags where the CLI has them (Claude `--max-budget-usd`, Copilot `--max-ai-credits`, Antigravity `--print-timeout`), total API-spend cap, `mesh halt`. Hooks prevent accidents; they are not a security sandbox.

## Usage & cost honesty
Tokens/cost are recorded per (worker, CLI) series, tagged `reported | estimated | unavailable`. `[billing]` declares each CLI as `subscription | api | credits | unknown`: only `api` counts as **spend**; subscription usage is shown as "informational API-equivalent". Optional `[prices."<model>"]` enables labelled estimates. Antigravity exposes no cost/quota; Copilot tokens come from its OpenTelemetry file export if present.

## Verify the parsers against your real CLIs (first thing to do)
`mesh probe <claude|copilot|antigravity> --yes` runs one tiny headless turn and reports whether the session id, usage, cost and result were understood and how many lines were unparsed. Send me the saved `probe-output/*.stream.jsonl` if coverage is low.
