#!/bin/sh
# End-to-end demo with simulated agents (no AI CLI required): decompose -> parallel claim -> isolate ->
# interrupt -> cross-CLI resume -> integrate. Run: examples/demo_workflow.sh
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; cd "$T"
git init -q -b main; echo "# demo" > README.md; git add -A; git -c user.email=d@d -c user.name=demo commit -qm init
"$HERE/bin/mesh" init --no-sync >/dev/null
M=.mesh/bin/mesh
sed -i.bak 's|^test = ".*"|test = "true"|' .mesh/mesh.toml && rm .mesh/mesh.toml.bak
g() { git -c user.email=d@d -c user.name=demo "$@"; }

echo "## 1. Orchestrator (claude) decomposes the work"
CL=$($M session start --cli claude --agent orchestrator --native-id orch0001 2>/dev/null)
EPIC=$($M task create "Todo app" --kind epic --as $CL)
API=$($M task create "REST API" --parent $EPIC --files "api/**" --agent backend --as $CL)
UI=$($M task create "UI" --parent $EPIC --files "ui/**" --agent frontend --as $CL)
DB=$($M task create "Schema" --parent $EPIC --files "db/**" --agent database --as $CL)
TST=$($M task create "E2E tests" --parent $EPIC --dep $API --dep $UI --files "tests/**" --agent qa --as $CL)
$M task tree

echo; echo "## 2. Three different CLIs claim independent tasks in parallel (E2E waits on deps)"
CO=$($M session start --cli copilot --agent frontend --native-id cop00001 2>/dev/null)
AG=$($M session start --cli antigravity --agent database --native-id agy00001 2>/dev/null)
$M task claim $API --as $CL; $M task claim $UI --as $CO; $M task claim $DB --as $AG
$M task claim $TST --as $CL || echo "(expected) blocked by dependencies"
echo "--- collision: copilot tries the API task"; $M task claim $API --as $CO || echo "(expected) refused"

echo; echo "## 3. Each works in an isolated worktree"
for pair in "$API:api:$CL" "$UI:ui:$CO" "$DB:db:$AG"; do
  id=${pair%%:*}; rest=${pair#*:}; dir=${rest%%:*}; who=${rest#*:}
  $M task start $id --as $who
  wt=$($M worktree create $id); mkdir -p "$wt/$dir"; echo "$dir work" > "$wt/$dir/main.txt"
  (cd "$wt" && git add -A && g commit -qm "$dir")
done

echo; echo "## 4. Antigravity hits its limit mid-task; copilot picks it up in a new CLI"
$M checkpoint create --task $DB --as $AG --done "schema drafted" --next "add indexes" --decision "sqlite" >/dev/null
$M session end --as $AG
$M msg send --as $CL --to "*" --type info "agy session ended unexpectedly" >/dev/null
$M resume
$M task resume $DB --as $CO
$M resume $DB --as $CO | sed -n '1,14p'

echo; echo "## 5. Integrate"
for id in $API $UI $DB; do $M integrate $id; done
echo; $M task list --all
echo; $M dash --once | head -20
echo; echo "demo repo: $T"
