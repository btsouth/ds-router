#!/bin/sh
# ============================================================================
# ds-router — uninstall.sh
#
# Reverses exactly what install.sh did, and nothing more:
#
#     ./uninstall.sh              stop and remove the timer/service + symlink
#     ./uninstall.sh --dry-run    print every action, change nothing
#     ./uninstall.sh --keep-symlink   leave ~/.local/bin/ds-switch in place
#     ./uninstall.sh --force      remove unit files even if modified by hand
#     ./uninstall.sh --help
#
# Deliberately NOT removed:
#   * the git checkout (this directory) and everything in it, including
#     config.yaml, __pycache__, docs/ — delete the clone yourself if you want
#     it gone
#   * anything under ~/.hermes (your Hermes config and API keys)
#   * the pyyaml package, if install.sh installed it (remove with
#     python3 -m pip uninstall pyyaml)
#   * quota snapshots under the collector state dir
#
# POSIX sh only: no bash arrays, no GNU-only flags.
# ============================================================================
set -eu


DRY_RUN=0
KEEP_SYMLINK=0
FORCE=0

hdr()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
skip() { printf '  skip  %s\n' "$*"; }
note() { printf '  note  %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  # Print the leading comment block, stopping at the first non-comment line.
  sed -n '3,/^[^#]/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' \
    | sed '/^=\+$/d'
}

while [ $# -gt 0 ]; do
  case $1 in
    --dry-run)      DRY_RUN=1 ;;
    --keep-symlink) KEEP_SYMLINK=1 ;;
    --force)        FORCE=1 ;;
    # Accepted for symmetry with install.sh; uninstall has no interactive prompt
    # that needs answering, so this is a no-op rather than an error.
    -y|--yes)       : ;;
    -h|--help)      usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# locate the checkout, same symlink-free logic as install.sh
# ---------------------------------------------------------------------------
: "${HOME:?HOME is not set — run this from a normal login shell}"
command -v dirname >/dev/null 2>&1 || {
  printf 'ERROR: the standard shell tools (dirname, sed, cmp, rm, readlink) must be on PATH.\n' >&2
  exit 1
}

self=$0
while [ -h "$self" ]; do
  link=$(readlink "$self")
  case $link in
    /*) self=$link ;;
    *)  self=$(dirname "$self")/$link ;;
  esac
done
# shellcheck disable=SC1007  # deliberate env override for cd, not a typo.
ROUTER_DIR=$(CDPATH= cd "$(dirname "$self")" && pwd -P)

CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
STATE_HOME=${XDG_STATE_HOME:-$HOME/.local/state}
SYSTEMD_USER_DIR=$CONFIG_HOME/systemd/user
STATE_DIR=$STATE_HOME/ds-router
MANIFEST=$STATE_DIR/install-manifest
BIN_DIR=$HOME/.local/bin
SYMLINK=$BIN_DIR/ds-switch
LAUNCHD_PLIST=$HOME/Library/LaunchAgents/com.ds-router.switch.plist

# ---------------------------------------------------------------------------
# manifest: written by install.sh, but never required. If it is missing we
# fall back to the same paths install.sh would have used, so an uninstall still
# cleans up after an older install or a hand-copied unit.
# ---------------------------------------------------------------------------
# Read the manifest as DATA. It used to be sourced, which meant a clone path
# containing & or | ran as shell code, and a path containing a space truncated the
# value and aborted the uninstall having removed nothing. Values are now taken
# literally from the first '=' onward, so every character round-trips. Only the
# keys this script needs are read; an unknown key is ignored.
manifest_load() {
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in ''|'#'*) continue ;; esac
    case $line in *=*) ;; *) continue ;; esac
    mkey=${line%%=*}
    mval=${line#*=}
    # Tolerate a value the older installer wrote shell-quoted.
    case $mval in
      \'*\') mval=${mval#\'}; mval=${mval%\'} ;;
    esac
    case $mkey in
      ROUTER_DIR)        ROUTER_DIR=$mval ;;
      SYSTEMD_USER_DIR)  SYSTEMD_USER_DIR=$mval ;;
      SYMLINK)           SYMLINK=$mval ;;
      LAUNCHD_PLIST)     LAUNCHD_PLIST=$mval ;;
      HERMES_BIN)        HERMES_BIN=$mval ;;
    esac
  done < "$1"
}

FROM_MANIFEST=0
if [ -f "$MANIFEST" ]; then
  manifest_load "$MANIFEST"
  FROM_MANIFEST=1
fi
SYSTEMD_USER_DIR=${SYSTEMD_USER_DIR:-$CONFIG_HOME/systemd/user}
SYMLINK=${SYMLINK:-$HOME/.local/bin/ds-switch}
BIN_DIR=$(dirname "$SYMLINK")

hdr "ds-router uninstaller"
say() { printf '%s\n' "$*"; }
say "  project        : $ROUTER_DIR"
say "  timer/service  : $SYSTEMD_USER_DIR/ds-router.{timer,service}"
say "  symlink        : $SYMLINK"
if [ "$FROM_MANIFEST" = 1 ]; then
  ok "using the record install.sh left at $MANIFEST"
else
  note "no manifest at $MANIFEST — using default locations"
fi
if [ "$DRY_RUN" = 1 ]; then
  note "DRY RUN: nothing will be stopped, removed or written"
fi

TMP_UNIT=${TMPDIR:-/tmp}/ds-router-uninstall.$$.unit
cleanup() { rm -f "$TMP_UNIT"; }
trap cleanup EXIT INT TERM HUP

escape_sed() { printf '%s' "$1" | sed 's/[&\\|]/\\&/g'; }

# Render what install.sh would have written, so a hand-edited unit is never
# silently destroyed by an uninstall.
render_expected() {
  src=$1
  hermes_bin=${HERMES_BIN:-}
  if [ -n "$hermes_bin" ] && [ -x "$hermes_bin" ]; then
    hermes_bin_dir=$(dirname "$hermes_bin")
  elif command -v hermes >/dev/null 2>&1; then
    hermes_bin_dir=$(dirname "$(command -v hermes)")
  else
    hermes_bin_dir=$HOME/.hermes/hermes-agent/venv/bin
  fi
  case $hermes_bin_dir in
    "$HOME"/*) hermes_bin_pat="%h/${hermes_bin_dir#"$HOME"/}" ;;
    *)         hermes_bin_pat=$hermes_bin_dir ;;
  esac
  case $ROUTER_DIR in
    "$HOME"/*) router_pat="%h/${ROUTER_DIR#"$HOME"/}" ;;
    *)         router_pat=$ROUTER_DIR ;;
  esac
  sed -e "s|%h/Projects/ds-router|$(escape_sed "$router_pat")|g" \
      -e "s|%h/.hermes/hermes-agent/venv/bin|$(escape_sed "$hermes_bin_pat")|g" \
      "$src"
}

have_systemctl=0
command -v systemctl >/dev/null 2>&1 && have_systemctl=1

# ---------------------------------------------------------------------------
# 1. systemd: stop, disable, remove, reload
# ---------------------------------------------------------------------------
hdr "Service / timer"
removed_any=0

unit_removable() {
  # $1 = installed unit path, $2 = template in the checkout
  #   0 = ours and safe to delete, 1 = not ours
  [ "$FORCE" = 1 ] && return 0
  if [ ! -f "$2" ]; then
    # No template to compare against (checkout moved or was edited); the unit
    # name is unique to this project, so it is still ours to remove.
    return 0
  fi
  render_expected "$2" >"$TMP_UNIT"
  cmp -s "$TMP_UNIT" "$1" && return 0
  return 1
}

for unit in ds-router.timer ds-router.service; do
  installed=$SYSTEMD_USER_DIR/$unit
  template=$ROUTER_DIR/systemd/$unit
  [ -f "$installed" ] || { skip "not installed: $installed"; continue; }

  if ! unit_removable "$installed" "$template"; then
    warn "$installed does not match what install.sh writes (hand-edited?)"
    warn "leaving it in place — remove it yourself or re-run with --force"
    continue
  fi

  if [ "$have_systemctl" = 1 ]; then
    case $unit in
      *.timer)
        if [ "$DRY_RUN" = 1 ]; then
          printf '  dry   systemctl --user disable --now %s\n' "$unit"
        else
          printf '  run   systemctl --user disable --now %s\n' "$unit"
          systemctl --user disable --now "$unit" >/dev/null 2>&1 \
            || warn "could not disable $unit (not loaded?); removing the file anyway"
        fi
        ;;
      *)
        if [ "$DRY_RUN" = 1 ]; then
          printf '  dry   systemctl --user stop %s  (never enabled)\n' "$unit"
        else
          printf '  run   systemctl --user stop %s  (never enabled)\n' "$unit"
          systemctl --user stop "$unit" >/dev/null 2>&1 || true
        fi
        ;;
    esac
  else
    skip "systemctl not present; skipping the systemd calls for $unit"
  fi

  if [ "$DRY_RUN" = 1 ]; then
    printf '  dry   rm -f %s\n' "$installed"
  else
    rm -f "$installed"
    printf '  rm    %s\n' "$installed"
  fi
  removed_any=1
done

if [ "$have_systemctl" = 1 ] && [ "$removed_any" = 1 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    printf '  dry   systemctl --user daemon-reload\n'
  else
    printf '  run   systemctl --user daemon-reload\n'
    systemctl --user daemon-reload >/dev/null 2>&1 || warn "daemon-reload failed (continuing)"
  fi
fi

# systemctl resets a failed unit's state on reload, but clear the counter too so
# a later reinstall starts clean.
if [ "$have_systemctl" = 1 ] && [ "$DRY_RUN" = 0 ] && [ "$removed_any" = 1 ]; then
  systemctl --user reset-failed ds-router.service >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 2. launchd (macOS)
# ---------------------------------------------------------------------------
if [ -f "$LAUNCHD_PLIST" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    printf '  dry   launchctl bootout gui/%s %s  (if loaded)\n' "$(id -u)" "$LAUNCHD_PLIST"
    printf '  dry   rm -f %s\n' "$LAUNCHD_PLIST"
  else
    if command -v launchctl >/dev/null 2>&1; then
      printf '  run   launchctl bootout gui/%s %s  (if loaded)\n' "$(id -u)" "$LAUNCHD_PLIST"
      launchctl bootout "gui/$(id -u)" "$LAUNCHD_PLIST" >/dev/null 2>&1 \
        || launchctl unload -w "$LAUNCHD_PLIST" >/dev/null 2>&1 \
        || true
    fi
    rm -f "$LAUNCHD_PLIST"
    printf '  rm    %s\n' "$LAUNCHD_PLIST"
  fi
else
  skip "no launchd plist at $LAUNCHD_PLIST"
fi

# ---------------------------------------------------------------------------
# 3. symlink — only if it is ours
# ---------------------------------------------------------------------------
hdr "Command-line convenience"
if [ "$KEEP_SYMLINK" = 1 ]; then
  skip "--keep-symlink: leaving $SYMLINK alone"
elif [ -L "$SYMLINK" ]; then
  target=$(readlink "$SYMLINK")
  if [ "$target" = "$ROUTER_DIR/ds-switch" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      printf '  dry   rm -f %s  (-> %s)\n' "$SYMLINK" "$target"
    else
      rm -f "$SYMLINK"
      printf '  rm    %s  (-> %s)\n' "$SYMLINK" "$target"
    fi
  else
    warn "$SYMLINK points at $target, not this checkout — leaving it alone"
    warn "(run uninstall.sh from the checkout it was installed from, or rm it yourself)"
  fi
elif [ -e "$SYMLINK" ]; then
  warn "$SYMLINK exists and is not a symlink — leaving it alone"
else
  skip "no symlink at $SYMLINK"
fi

# ---------------------------------------------------------------------------
# 4. manifest
# ---------------------------------------------------------------------------
hdr "Records"
if [ -f "$MANIFEST" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    printf '  dry   rm -f %s\n' "$MANIFEST"
    printf '  dry   rmdir %s  (if empty)\n' "$STATE_DIR"
  else
    rm -f "$MANIFEST"
    printf '  rm    %s\n' "$MANIFEST"
    # if/else rather than `A && B || C`: the latter also runs the else branch
    # when B fails, not only when A does.
    if rmdir "$STATE_DIR" 2>/dev/null; then
      printf '  rmdir %s\n' "$STATE_DIR"
    else
      note "$STATE_DIR kept (not empty — logs or other state remain)"
    fi
  fi
else
  skip "no manifest at $MANIFEST"
fi
note "quota snapshots and Hermes' own state are untouched"

# ---------------------------------------------------------------------------
# 5. what is left, on purpose
# ---------------------------------------------------------------------------
hdr "Left in place on purpose"
say "  the checkout itself : $ROUTER_DIR   (git repo, config.yaml, docs/ — delete it yourself if you want it gone)"
say "  Hermes config/keys  : ~/.hermes/ (and \$HERMES_HOME if set)"
say "  pyyaml, if installed: python3 -m pip uninstall pyyaml"
say ""
say "  ds-router is now inert: nothing polls, and Hermes keeps whatever provider"
say "  it was last set to. To hand routing back to your config's default first:"
say "      cd $ROUTER_DIR && ./ds-switch --off"
if [ "$DRY_RUN" = 1 ]; then
  say ""
  say "  DRY RUN — nothing above was stopped, removed or written."
fi
say ""
