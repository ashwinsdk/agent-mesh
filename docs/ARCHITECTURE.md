# Architecture

## Principles
One canonical state store, many thin client adapters. Continuity never depends on a CLI's conversation history: everything an agent
needs to continue is in `.mesh/`. Python stdlib only, no daemon, local-first.

## Layers
| Layer | Where | Committed? |
|---|---|---|
| Universal infrastructure | `.mesh/lib/mesh` (vendored copy of `src/mesh`), `.mesh/bin/mesh` | yes |
| Project config | `.mesh/mesh.toml` | yes |
| Canonical instructions | `.mesh/instructions/CORE.md` → managed block in `AGENTS.md`, import in `CLAUDE.md` | yes |
| Agent registry (14 roles) | `.mesh/agents/*.md` (your edits are never overwritten) | yes |
| Human-reviewable memory (layers A, D) | `.mesh/memory/project.md`, `decisions/ADR-*.md`, `lessons.md`, `rejected.md`, `debt.md` | yes |
| Client integrations (generated) | `.claude/`, `.github/hooks`, `.github/agents`, `.agents/hooks.json` | yes |
| Runtime state (layers B, C, E) | `.mesh/runtime/` (SQLite DB, checkpoints, backups, logs, worktrees) | **no** (gitignored) |

## Memory layers
- **A Global**: `memory/project.md` facts `[verified|assumed | date | author]`; written via `mesh mem fact` under a file lock; direct edits blocked by hook.
- **B Working**: `mesh mem working` regenerates from the DB (never hand-edited).
- **C Agent**: sessions/agent columns, per-agent messages and checkpoints.
- **D Long-term**: ADRs (monotonic numbering under lock), lessons, rejected approaches, debt.
- **E Session**: `sessions` row + latest checkpoint + `mesh resume`.
Safeguards: optimistic hash check on replace (`mem commit --expect`), snapshots in `runtime/backups/memory`, secret scan before write, staleness report (age, unverified, missing referenced files), banner "source code outranks memory".

## Concurrency model
SQLite WAL + `BEGIN IMMEDIATE` for every write: the write lock is taken before reading, so check-then-update sequences (claim, lock acquire) are atomic across processes.
`task claim` = dependency check + file-lock acquisition + conditional `UPDATE … WHERE status='READY' AND session_id IS NULL` in one transaction.
Locks: glob/path with conservative overlap test, TTL + heartbeat, recovery when owner session ended or local PID is gone.
Edits: PreToolUse hook denies edits to paths locked by another session. Overlapping work uses `mesh worktree create` (branch `mesh/<task>`).
Integration (`mesh integrate`): precheck clean worktree → run `[validation].test` on the task branch → `git merge-tree` conflict detection → merge into `mesh/integration` worktree → re-run tests (revert the throwaway integration branch on failure) → task COMPLETED. Conflicts become `conflict` messages + `NEEDS_REWORK`; nothing is discarded. Promotion to `main` is a human command (`mesh promote`).

## Context lifecycle
`mesh context brief <task>` (ranked, budgeted) → statusline (Claude, real %) or estimate → at `warn_pct` warn, at `checkpoint_pct` auto-checkpoint, PreCompact hook checkpoints before compaction → `mesh rollover [--launch cli]` writes a checkpoint, ends the session, prints/launches a fresh session with the continuation brief. A discarded context is never "recovered"; the persisted state is.

## Usage honesty
Every `usage` row has `source ∈ {reported, estimated, unavailable}` and NULL for unknown fields. Dashboard shows R/E/- badges and "unavailable from this CLI".

## Safety
Secret regex + entropy on messages/memory/checkpoints/writes; destructive-command denylist; protected paths (`.mesh/mesh.toml`, instructions, agents, `project.md`, `.git/`); hooks fail open on internal errors (logged to `runtime/logs/hook-errors.log`) and have a 2s DB budget; all message content is fenced as untrusted data when injected.

## Decisions
1. SQLite over files/daemon: atomic multi-row transactions without a service. 2. Python stdlib: zero install, vendored into the repo. 3. No daemon: liveness via heartbeat/TTL/PID. 4. One instruction source, generated per client (single `AGENTS.md` block; Claude imports it; no per-client duplication). 5. Worktrees for isolation, humans promote.
