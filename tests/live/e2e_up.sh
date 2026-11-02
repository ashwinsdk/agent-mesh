#!/bin/sh
# Prepare a scratch repo for the full `mesh up` experience with a REAL CLI, then print what to run.
# Usage: tests/live/e2e_up.sh [claude|copilot|antigravity]
set -e
CLI="${1:-claude}"
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
T="${TMPDIR:-/tmp}/mesh-e2e-$(date +%s)"; mkdir -p "$T"; cd "$T"
git init -q -b main && echo "# e2e" > README.md && echo '{"name":"e2e","scripts":{"test":"echo ok"}}' > package.json
git add -A && git -c user.email=e@e -c user.name=e commit -qm init
"$HERE/bin/mesh" init >/dev/null
echo "Scratch project: $T"
echo
echo "1) In terminal A:   cd $T && .mesh/bin/mesh probe $CLI --yes        # verifies parsing against the real CLI first"
echo "2) In terminal B:   cd $T && .mesh/bin/mesh dash                    # live dashboard (tabs 1-6)"
echo "3) In terminal A:   cd $T && .mesh/bin/mesh up $CLI \"Create a tiny static website with three pages (home, about, contact) and a shared stylesheet\""
echo "   (first run asks you to accept the autonomy policy; the orchestrator then plans, asks for your go-ahead, and spawns workers)"
echo "4) Emergency stop any time:  .mesh/bin/mesh halt"
