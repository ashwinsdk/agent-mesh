# agent-mesh

Turns any git repository into a coordinated multi-agent workspace for **Claude Code, Antigravity (`agy`) and GitHub Copilot CLI** (and anything that can read `AGENTS.md` / run shell commands). Shared tasks, memory, messages, file locks, checkpoints and honest usage tracking live in the repo (`.mesh/`), not in any CLI's chat history. Python 3.11+ stdlib only; macOS/Linux.

## Install into a project
```sh
/path/to/agent-mesh/bin/mesh init        # in the target repo; or: /path/to/agent-mesh/install.sh /path/to/repo
.mesh/bin/mesh doctor                    # capability matrix, integrity, installed clients
git add .mesh AGENTS.md CLAUDE.md .claude .github .agents .gitignore
```
`init` detects the stack (npm, pyproject, cargo, go, …), writes `.mesh/mesh.toml`, vendors the tool into `.mesh/lib` (the repo is self-contained), installs 14 agent roles, and generates client config. Existing `CLAUDE.md`/`AGENTS.md`/`.claude/settings.json`/agent files are merged or skipped, never overwritten; hand-edited managed blocks produce `*.mesh-proposed` instead.

## Daily use
```sh
mesh task create "Add login" --files "src/auth/**" --accept "tests pass"   # orchestrator or human
mesh task ready                         # what can start now (deps satisfied)
mesh task claim T-xxxx --as <session>   # atomic; takes file locks; fails if owned/overlapping
mesh worktree create T-xxxx             # isolated branch mesh/T-xxxx
mesh checkpoint create --done "…" --next "…" --decision "…"
mesh msg send --to cli:claude --type question "which auth header?"
mesh integrate T-xxxx                   # validate → conflict check → merge → re-validate
mesh dash                               # live TUI (q to quit); --once for plain text
mesh usage report                       # JSON; source = reported | estimated | unavailable
```
Agents get their session id and protocol automatically via SessionStart hooks and `AGENTS.md`. Launch several terminals (any mix of CLIs) in the same repo; run `mesh run <claude|copilot|antigravity> --task T-xxxx` for a wrapper that registers the session, resumes a task and checkpoints on crash/exit (useful for Antigravity, which has no SessionEnd hook).

## Recovery
- Interrupted task: `mesh resume` (lists), `mesh task resume T-xxxx`, `mesh resume T-xxxx` (brief with checkpoint + repo-drift warnings). Works from a different CLI.
- Context full: `mesh rollover [--launch copilot]`.
- Corrupt DB: `mesh doctor --repair` / `mesh restore` (uses `mesh backup` snapshots). Corrupt checkpoint: automatically falls back to the previous valid one.
- Stale locks: expire by TTL and are reclaimed when the owner session ended or its PID died; `mesh task reap` returns abandoned tasks to READY.

## Docs
[Findings & capability matrix](docs/FINDINGS.md) · [Architecture](docs/ARCHITECTURE.md) · [Acceptance status & limits](docs/ACCEPTANCE.md) · [Adding a CLI adapter](docs/ADDING_ADAPTERS.md) · `examples/demo_workflow.sh`

## Configuration (`.mesh/mesh.toml`)
`[validation].test` is what `integrate` runs (auto-detected, edit it). `[context]` thresholds, `[locks].ttl_seconds`, `[tasks].stale_after_seconds`, `[policy]` (`destructive`, `secret_in_write`, `protect_mesh_files`). Change agent roles by editing `.mesh/agents/*.md` then `mesh sync`.

## Troubleshooting
- Hooks seem inactive: `mesh doctor` ("hooks seen live here"), check `.mesh/runtime/logs/hook-errors.log`; hooks fail open by design.
- Edit denied unexpectedly: `mesh lock list`; ask the owner to release, or `mesh msg send --type file_request`.
- `mesh: not inside a mesh project`: run from within the repo or set `MESH_ROOT`.
- Customised statusline: mesh keeps yours; chain `.mesh/bin/mesh statusline` to get Claude usage/context tracking.
- Update the vendored copy after pulling a new agent-mesh: `mesh upgrade && mesh sync`.

## Tests
`python3 -m unittest discover -s tests` · live CLI check: `tests/live/verify_cli.sh` (spends a few tokens; run by you).
