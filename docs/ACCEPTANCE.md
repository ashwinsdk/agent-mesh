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

---
# v2 (orchestration) status

Run: `python3 -m unittest discover -s tests` (73 tests, ~65 s). All worker/orchestrator tests use **real runner processes driving a fake CLI** (`tests/fakecli/fake.py`) that emits claude / antigravity / copilot style streams; the real CLIs were **not** launched by the automated suite.

| Requirement | Evidence (test) | Status |
|---|---|---|
| Dynamic creation of real independent sessions (detached processes, own process group, pid/start-time tracked) | `TestSpawn`, `test_five_parallel_workers_and_caps` | PASS (fake CLI) |
| Agent vs session vs process distinction | `workers` table + dashboard columns; `TestEndToEnd` | PASS |
| Spawn caps, duplicate-spawn prevention, atomic claim | `test_five_parallel…`, `test_duplicate_spawn…` | PASS |
| Crash / kill -9 detection and safe restart | `test_kill9_runner_group_detected_and_task_resumed`, e2e zoro crash | PASS |
| Usage-limit → fallback CLI; no fallback → task released | `test_usage_limit_falls_back…`, `test_limit_without_fallback…` | PASS |
| Stall detection; exit-without-submit is not completion | `test_stall_is_detected…`, `test_exit_without_submit…` | PASS |
| Kill switch | `test_halt_kills_everything_and_blocks_spawns` | PASS |
| Worker env allow-list (no ambient secrets) | `test_worker_env_does_not_leak_ambient_secrets` | PASS |
| Completion gates (submit only; verify bound to commit; ownership; weak; high-risk independent review; workers can't approve/integrate/spawn/halt) | `TestGates` | PASS |
| Reject → worker resumed with feedback | `test_failing_verify_blocks_and_reject_restarts…` | PASS |
| Dependency routing (handoff to dependents' prompt + message) | `test_full_gate_flow_and_dependency_routing` | PASS |
| Mailbox/event injection per CLI format; retries; expiry; delivery failure; dedupe | `TestComms` | PASS (payload formats from vendor docs) |
| Event-driven wait (<2.6 s wake, ignores noise/own events, ~0 CPU idle) | `test_wait_wakes_fast…` | PASS |
| Hierarchical delegation needs orchestrator approval, depth-capped | `TestDelegation` | PASS |
| Decisions ratified by orchestrator only, broadcast + ADR | `test_decision_ratification…` | PASS |
| 6-task graph end to end (plan→3 parallel workers→verify→approve→integrate→report) | `test_six_task_graph_with_crash_recovery_completes` | PASS |
| Cost semantics (subscription ≠ spend; api spend cap) | `test_billing_mode_controls_spend`, scale test | PASS |
| `mesh up` prompt, autonomy opt-in, resume | `test_up_dry_run…`, `test_autonomy_accept…` | PASS (launch itself not exercised) |
| Dashboard: all fields present or marked unavailable; 60-worker render < 0.5 s CPU | `TestDashboardScale` | PASS (curses loop not exercised) |
| Read-only web viewer (405 on writes, Host guard, no innerHTML) | `TestWebViewer` | PASS |
| `mesh probe` parser-coverage report | `TestProbe` | PASS (against fake) |

## Not verified (needs you, with real CLIs)
1. **Stream/usage parsers vs real output**: `mesh probe <cli> --yes` for each CLI. Copilot's JSONL schema is undocumented; its parser is generic and its tokens rely on the OTel export.
2. **Headless runs load the project hooks** (needed for guards and mailbox injection) and honour `additionalContext`/`injectSteps` in `-p` mode.
3. **Copilot resume-by-id** (assumed unsupported → fresh session + checkpoint brief).
4. Interactive `mesh up` launch with each CLI (orchestrator prompt delivered as the initial prompt) and the live curses/web dashboards.
5. The orchestrator's own judgement (planning quality, when to spawn) depends on the model following the playbook; mesh enforces the gates, not the reasoning.
6. Orchestrator cannot be pushed events mid-turn: it relies on `mesh wait` and hook digests at tool boundaries.

---
# v3 (app + conductor) status
Run: `python3 -m unittest discover -s tests` (core, no Textual needed) and, to include the app tests, `python -m venv v && v/bin/pip install textual && v/bin/python -m unittest discover -s tests`.
| Your issue | Fix | Evidence |
|---|---|---|
| 1. every `mesh up` made an orchestrator; no workers | exactly one active run and one orchestrator row; older orchestrators are retired; stale/ghost sessions age out and are hidden; workers spawned by code | `test_stale_sessions_age_out_and_orchestrator_is_singular`, `test_orchestrator_is_one_row_and_no_ghost_sessions` |
| 3. tasks created but never executed | the conductor dispatches ready tasks itself; spawn failures are visible (`dispatch.error` + attention item) | `test_REGRESSION_tasks_created_but_never_executed`, `test_dispatch_failure_is_visible_not_silent`, `test_prompt_to_complete_with_no_llm_driving_execution` |
| 2/4. clean interactive TUI | `mesh` opens a Textual app (activity, agents, tasks, usage, needs-you, drill-downs, wizard) | `tests/test_app.py` (pilot): layout at 80×24/120×40/200×50, wizard, F-keys, agent view, full prompt→plan→approve→complete flow through the UI, re-attach after quit |
| rework / escalation / failure policy | auto verify → approve/merge (low risk, strong checks), rework ≤ `max_rework`, escalate; attempts cap; weak/high-risk ask the human | `TestJudgement` |
| plan validity and brain safety | schema + cycle/ownership/agent validation with one repair retry; brain actions whitelisted | `TestPlanner`, `test_invalid_brain_actions_are_skipped_and_surfaced` |
| conductor lifecycle | single instance (flock), survives UI exit, kill -9 recovery | `TestLifecycle` |
| packaging | Textual only in the app; vendored project copy and hooks never import it | `TestPackaging` |

## Not verified (needs you, with real CLIs)
1. `mesh probe <cli> --planner --yes`: real planner output (Claude `structured_output`, Antigravity `--json-schema` result, Copilot fenced JSON) has not been captured; parsers follow docs and tolerate text/fenced JSON.
2. `mesh probe <cli> --hooks --yes` (headless hooks) is still pending from earlier.
3. The live look and feel in a real terminal (tested headlessly with Textual's pilot; colours, mouse and very small terminals not exercised).
4. Auto-approval trusts the project's verify commands; weak verification always asks you.
