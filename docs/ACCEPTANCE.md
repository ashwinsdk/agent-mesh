# Acceptance status

Run: `python3 -m unittest discover -s tests` (37+ tests, ~15 s) and `examples/demo_workflow.sh`.
"Simulated" = the CLIs are represented by mesh sessions/hook payloads built from the vendors' documented schemas; the real binaries were **not** run by the automated suite.

| Scenario | Evidence | Status | Limits |
|---|---|---|---|
| A Parallel development (3 sessions, 3 tasks) | `TestGitFlow.test_scenario_A…` (3 worktrees, 3 CLIs, integrate all, no overwrite); demo step 3/5 | PASS (simulated sessions) | real model sessions not run |
| B Cross-CLI communication | `TestBus.test_cross_cli_messaging_ack_dedupe`, broadcast ack, injection fencing | PASS | delivery to a live agent needs its SessionStart hook or `mesh msg inbox` |
| C Session interruption → resume elsewhere | `TestRecovery.test_scenario_C_and_G…` incl. repo-drift warning | PASS | agent must follow AGENTS.md to checkpoint; auto-checkpoints only on pre-compact (Claude/Copilot), context threshold, `mesh run` exit |
| D Context exhaustion | `TestContext.test_scenario_D…`: warn → auto-checkpoint → rollover → fresh session resumes | PASS | context % is *reported* only for Claude; others need an estimate source |
| E Task collision / protected files | `test_scenario_E_*` (8-process claim race → exactly 1 winner; overlapping-file claim refused and audited; 6-process lock race; hook denials) | PASS | hook denial depends on each CLI honouring PreToolUse; shell-level edits bypass it |
| F Usage monitoring | `TestUsage.test_scenario_F…`: reported vs estimated vs unavailable shown in report and dashboard | PASS (Claude payload per docs) | live statusline not run; Antigravity/Copilot tokens & quota are unavailable by design |
| G CLI independence | same DB/tasks/memory used by claude/copilot/antigravity sessions in tests and demo | PASS (simulated) | |
| H Project portability | `TestPortability` (python, rust, go, node, empty repos; vendored shim runs without source tree; existing CLAUDE.md/AGENTS.md/settings/agents preserved) | PASS | |

Other tested: state machine, dependencies/cycles, hierarchy progress, abandoned-task reassignment (live owner cannot be stolen from), lock expiry and dead-session recovery, duplicate message protection, memory optimistic locking / concurrent appends / ADR numbering / secret refusal / staleness, checkpoint corruption fallback, DB corruption restore, merge conflicts, failing validation, dirty worktree, hook fail-open, rate-limit event recording, missing usage data.

## Not verified / known gaps (honest list)
- Real Claude/Copilot/Antigravity hook invocation: run `tests/live/verify_cli.sh`.
- Live curses dashboard rendering (the data function `snapshot` is tested; the curses loop is not).
- Machine reboot is covered only as "new process sees persisted state".
- No automatic token/context collector for Antigravity or Copilot.
- Windows is unsupported (`fcntl`, shell shims).
- Hook enforcement is not a security boundary.
