#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HERMES_ROOT=${HERMES_HOME:-"$HOME/.hermes"}
TARGET="$HERMES_ROOT/skills/chip-grok"

if [ -e "$TARGET" ]; then
  BACKUP="$TARGET.backup.$(date +%Y%m%d%H%M%S)"
  mv "$TARGET" "$BACKUP"
  printf 'Backed up existing skill to %s\n' "$BACKUP"
fi

mkdir -p "$TARGET"
for path in SKILL.md README.md LICENSE references scripts; do
  cp -R "$SOURCE_DIR/$path" "$TARGET/"
done
find "$TARGET" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$TARGET" -type f -name '*.pyc' -delete
chmod 700 "$TARGET/scripts/install.sh" "$TARGET/scripts/test.sh"
chmod 700 "$TARGET/scripts/chip_grok.py"

if command -v hermes >/dev/null 2>&1; then
  HERMES_HOME="$HERMES_ROOT" hermes config set quick_commands.grok.type alias --force >/dev/null
  HERMES_HOME="$HERMES_ROOT" hermes config set quick_commands.grok.target /chip-grok --force >/dev/null
  HERMES_HOME="$HERMES_ROOT" hermes config set quick_commands.grok.description 'Run Grok Build as an isolated coding worker' --force >/dev/null
  printf 'Configured /grok -> /chip-grok\n'
fi

printf 'Installed chip-grok at %s\n' "$TARGET"
printf 'Run /reload-skills in a live Hermes gateway session.\n'
