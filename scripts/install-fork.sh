#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COMMAND=${1:-}
[ -n "$COMMAND" ] || { printf 'usage: %s install|rollback [options]\n' "$0" >&2; exit 2; }
shift
ROOT=${CHIP_GROK_INSTALL_ROOT:-"$HOME/.local/lib/chip-grok"}
BINARY=
LOCK=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) ROOT=$2; shift 2 ;;
    --binary) BINARY=$2; shift 2 ;;
    --lock) LOCK=$2; shift 2 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

verify() {
  python3 "$SCRIPT_DIR/verify-fork.py" --binary "$1" --lock "$2" >/dev/null
}

validated_tag() {
  python3 - "$1" <<'PY'
import json, re, sys
payload = json.load(open(sys.argv[1]))
tag = payload.get("tag")
if not isinstance(tag, str) or re.fullmatch(r"chip-v[0-9A-Za-z._-]+", tag) is None or "/" in tag:
    raise SystemExit("invalid fork release tag")
print(tag)
PY
}

validated_target() {
  local check_target=$1 check_tag check_resolved releases_resolved lock_tag
  case "$check_target" in
    releases/chip-v*) ;;
    *) return 1 ;;
  esac
  check_tag=${check_target#releases/}
  [ -n "$check_tag" ] && [ "$check_target" = "releases/$check_tag" ] || return 1
  case "$check_tag" in */*|.|..) return 1 ;; esac
  [ -d "$ROOT/releases/$check_tag" ] || return 1
  check_resolved=$(CDPATH= cd -- "$ROOT/releases/$check_tag" 2>/dev/null && pwd -P) || return 1
  releases_resolved=$(CDPATH= cd -- "$ROOT/releases" 2>/dev/null && pwd -P) || return 1
  [ "$check_resolved" = "$releases_resolved/$check_tag" ] || return 1
  lock_tag=$(validated_tag "$ROOT/$check_target/fork.lock.json") || return 1
  [ "$lock_tag" = "$check_tag" ] || return 1
}

verify_target() {
  validated_target "$1" || return 1
  verify "$ROOT/$1/grok" "$ROOT/$1/fork.lock.json"
}

case "$COMMAND" in
  install)
    [ -n "$BINARY" ] && [ -n "$LOCK" ] || { printf 'install requires --binary and --lock\n' >&2; exit 2; }
    verify "$BINARY" "$LOCK"
    TAG=$(validated_tag "$LOCK")
    mkdir -p "$ROOT/releases"
    chmod 700 "$ROOT" "$ROOT/releases"
    RELEASE="$ROOT/releases/$TAG"
    if [ -e "$RELEASE" ]; then
      verify "$RELEASE/grok" "$RELEASE/fork.lock.json"
      cmp -s "$LOCK" "$RELEASE/fork.lock.json" || { printf 'release tag already contains a different lock\n' >&2; exit 1; }
    else
      STAGE=$(mktemp -d "$ROOT/releases/.stage.XXXXXX")
      trap 'rm -rf "${STAGE:-}"' EXIT
      install -m 700 "$BINARY" "$STAGE/grok"
      install -m 600 "$LOCK" "$STAGE/fork.lock.json"
      verify "$STAGE/grok" "$STAGE/fork.lock.json"
      mv "$STAGE" "$RELEASE"
      STAGE=
      trap - EXIT
    fi
    if [ -L "$ROOT/current" ]; then
      OLD=$(readlink "$ROOT/current")
      verify_target "$OLD" || { printf 'invalid or unverified current release target\n' >&2; exit 1; }
      PREVIOUS_TMP="$ROOT/.previous.$$"
      ln -s "$OLD" "$PREVIOUS_TMP"
      mv -Tf "$PREVIOUS_TMP" "$ROOT/previous"
    elif [ -e "$ROOT/current" ]; then
      printf 'current must be a symlink\n' >&2; exit 1
    fi
    CURRENT_TMP="$ROOT/.current.$$"
    ln -s "releases/$TAG" "$CURRENT_TMP"
    mv -Tf "$CURRENT_TMP" "$ROOT/current"
    printf 'Installed verified fork release %s at %s\n' "$TAG" "$RELEASE"
    ;;
  rollback)
    [ -z "$BINARY" ] && [ -z "$LOCK" ] || { printf 'rollback accepts only --root\n' >&2; exit 2; }
    [ -L "$ROOT/previous" ] || { printf 'no previous fork release is available\n' >&2; exit 1; }
    TARGET=$(readlink "$ROOT/previous")
    verify_target "$TARGET" || { printf 'invalid or unverified previous release target\n' >&2; exit 1; }
    RELEASE="$ROOT/$TARGET"
    CURRENT_OLD=
    if [ -L "$ROOT/current" ]; then
      CURRENT_OLD=$(readlink "$ROOT/current")
      verify_target "$CURRENT_OLD" || { printf 'invalid or unverified current release target\n' >&2; exit 1; }
    elif [ -e "$ROOT/current" ]; then
      printf 'current must be a symlink\n' >&2; exit 1
    fi
    CURRENT_TMP="$ROOT/.current.$$"
    ln -s "$TARGET" "$CURRENT_TMP"
    mv -Tf "$CURRENT_TMP" "$ROOT/current"
    if [ -n "$CURRENT_OLD" ]; then
      PREVIOUS_TMP="$ROOT/.previous.$$"
      ln -s "$CURRENT_OLD" "$PREVIOUS_TMP"
      mv -Tf "$PREVIOUS_TMP" "$ROOT/previous"
    fi
    printf 'Rolled back current fork to %s\n' "$TARGET"
    ;;
  *) printf 'usage: %s install|rollback [options]\n' "$0" >&2; exit 2 ;;
esac
