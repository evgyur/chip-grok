#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HERMES_ROOT=${HERMES_HOME:-"$HOME/.hermes"}
TARGET="$HERMES_ROOT/skills/chip-grok"
BACKUP_ROOT="$HERMES_ROOT/skill-backups"
TMP_BASE=${TMPDIR:-/tmp}
[ -d "$TMP_BASE" ] || TMP_BASE=/tmp
STAGE_ROOT=$(mktemp -d "$TMP_BASE/chip-grok-install.XXXXXX")
STAGE="$STAGE_ROOT/chip-grok"
trap 'rm -rf "$STAGE_ROOT"' EXIT

mkdir -p "$STAGE"
for path in SKILL.md README.md LICENSE fork.lock.json .gitignore .github aliases references scripts tests; do
  cp -R "$SOURCE_DIR/$path" "$STAGE/"
done
find "$STAGE" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$STAGE" -type f -name '*.pyc' -delete
chmod 700 "$STAGE/scripts/install.sh" "$STAGE/scripts/test.sh"
chmod 700 "$STAGE/scripts/chip_grok.py" "$STAGE/scripts/fork_contract.py" "$STAGE/scripts/verify-fork.py" "$STAGE/scripts/install-fork.sh" "$STAGE/scripts/update-fork.py"

# The source is now fully staged. This keeps self-update safe even when this
# installer is running from the currently active skill directory.
if [ -e "$TARGET" ]; then
  mkdir -p "$BACKUP_ROOT"
  BACKUP="$BACKUP_ROOT/chip-grok.$(date +%Y%m%d%H%M%S)"
  mv "$TARGET" "$BACKUP"
  printf 'Backed up existing skill to %s\n' "$BACKUP"
fi
mkdir -p "$(dirname "$TARGET")"
mv "$STAGE" "$TARGET"

printf 'Installed chip-grok at %s\n' "$TARGET"
printf 'Run /reload-skills in a live Hermes gateway session.\n'
