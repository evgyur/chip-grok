#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

bash -n scripts/*.sh
python3 -m py_compile scripts/*.py tests/*.py
python3 -m unittest discover -s tests -v
python3 scripts/public_hygiene.py
GUARD="$HOME/.hermes/skills/create-skill/scripts/skill_workflow_guard.py"
if [ -f "$GUARD" ]; then
  python3 "$GUARD" .
else
  printf 'SKILL_WORKFLOW_GUARD_SKIPPED (external Hermes guard not installed)\n'
fi
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git diff --check
else
  printf 'GIT_DIFF_CHECK_SKIPPED (installed package is not a git checkout)\n'
fi
find . -type d -name __pycache__ -prune -exec rm -rf {} +
find . -type f -name '*.pyc' -delete
printf 'ALL_TESTS_OK\n'
