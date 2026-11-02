#!/bin/sh
# Usage: install.sh [target-repo-dir]   (default: current directory). Idempotent.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v python3 >/dev/null || { echo "python3 (3.11+) required" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11+ required" >&2; exit 1; }
TARGET="${1:-$PWD}"
cd "$TARGET"
git rev-parse --git-dir >/dev/null 2>&1 || echo "warning: not a git repo; worktrees/integration need git" >&2
"$HERE/bin/mesh" init
