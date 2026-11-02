# Adding a CLI adapter

1. **Research first.** Find the client's official docs for: instruction files, hooks (event names, config path, stdin/stdout JSON, how to deny), agent definitions, session flags, and any usage metric. Record unknowns as `unverified`.
2. **Subclass `Adapter`** in `src/mesh/adapters.py`: set `name`, `binary`, honest `CAPS` (`native | adapter | external | estimated | unavailable | unverified`), implement `sync()` (generate files; use `upsert_block`/`write_owned` so user content is never clobbered) and `launch_cmd(prompt)` (used by `mesh rollover --launch` and `mesh run`). Register it in `ADAPTERS`.
3. **Payload mapping** in `hooks.normalize`: map the client's fields to `native` (session id), `tool`, `args`, `transcript`, `model`; add the response shape for deny/context in `hooks._out`.
4. **Hook events** map to: `session_start`, `session_end`, `pre_tool`, `post_tool`, `pre_compact`, `stop`, `error`. Implement only events the client really has; use `mesh run` for missing lifecycle.
5. **Usage**: if the client exposes tokens/context, add a mapper like `usage.from_claude_statusline` and record with `source="reported"`; otherwise leave unavailable or call `usage.estimate_from_file` (`estimated`).
6. **Tests**: add hook-payload tests (deny/allow/fail-open) to `tests/test_resilience.py::TestHooks` and a live check to `tests/live/verify_cli.sh`.
No changes are needed to tasks, memory, bus, locks, checkpoints or dashboard.
