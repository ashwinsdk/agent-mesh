#!/bin/sh
# Live smoke test: does each REAL CLI fire the generated mesh hooks? (Costs a few tokens per CLI; run it yourself.)
# Usage: tests/live/verify_cli.sh [claude|copilot|antigravity ...]   (default: all installed)
set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
T="$(mktemp -d)"; cd "$T"
git init -q -b main && echo hi > README.md && git add -A && git -c user.email=a@b -c user.name=t commit -qm init
"$HERE/bin/mesh" init >/dev/null
X=.mesh/bin/mesh
PROMPT='Create a file named hello.txt containing the word hi using your file tool, then say done.'
for cli in ${@:-claude copilot antigravity}; do
  case $cli in
    claude)      bin=claude;  cmd="claude -p \"$PROMPT\" --permission-mode acceptEdits" ;;
    copilot)     bin=copilot; cmd="copilot -p \"$PROMPT\" --allow-all-tools" ;;
    antigravity) bin=agy;     cmd="agy -p \"$PROMPT\" --dangerously-skip-permissions" ;;
  esac
  command -v $bin >/dev/null || { echo "[$cli] not installed, skipped"; continue; }
  echo "=== $cli: $cmd"
  rm -f hello.txt
  sh -c "timeout 180 $cmd" >/dev/null 2>&1 || true
  echo "sessions registered by hooks:"; $X session list | grep "^$cli" || echo "  NONE  -> sessionStart hook did not fire (or payload differs)"
  echo "tool events seen: $(sqlite3 .mesh/runtime/mesh.db "select count(*) from events where type='tool'")"
  echo "hook errors:"; cat .mesh/runtime/logs/hook-errors.log 2>/dev/null | head -5 || true
  echo "lock-deny test (another session owns hello.txt):"
  rm -f hello.txt
  $X session start --cli generic --native-id blocker00 >/dev/null 2>&1
  $X task create "own hello" --files hello.txt --id TL-$cli >/dev/null
  $X task claim TL-$cli --as generic-blocker0 >/dev/null
  sh -c "timeout 180 $cmd" >/dev/null 2>&1 || true
  [ -e hello.txt ] && echo "  FAIL: file was created despite lock (PreToolUse deny not honoured)" || echo "  PASS: edit denied (or file tool not used)"
  $X task release TL-$cli --as generic-blocker0 >/dev/null 2>&1
done
echo; $X doctor | sed -n '/Capability/,$p'
echo "workdir: $T"
