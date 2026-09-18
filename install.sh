#!/bin/sh
# ============================================================================
# ds-router — install.sh
#
# Get a fresh clone working as the current user, with one command:
#
#     ./install.sh               install (files + a user timer on Linux)
#     ./install.sh --dry-run     print every action, change nothing
#     ./install.sh --no-service  files only: no timer and no service unit
#     ./install.sh --no-symlink  do not create ~/.local/bin/ds-switch
#     ./install.sh --yes         non-interactive; assume yes to offered steps
#     ./install.sh --help
#
# Safe by default and idempotent: re-running it makes no further changes and
# never fails just because something is already installed. Nothing outside the
# user's own directories is touched. No sudo, ever.
#
# --dry-run writes nothing at all, but it does still run the read-only checks
# (the test suites and `ds-switch --check`) so you can see whether the tree is
# healthy before committing to anything.
#
# POSIX sh only — no bash arrays, no GNU-only flags — because this has to run
# under macOS's /bin/sh (bash 3.2) as well as Linux's dash.
#
# What it installs, exactly (all reversible with ./uninstall.sh):
#   1. a rendered copy of systemd/*.service and *.timer in the user's systemd
#      unit dir, enabled as ds-router.timer        (Linux, unless --no-service)
#   2. on macOS: a launchd plist in ~/Library/LaunchAgents, not loaded for you
#      (unless --no-service) — instructions are printed
#   3. an optional ~/.local/bin/ds-switch symlink  (unless --no-symlink)
#   4. a manifest of the above at $XDG_STATE_HOME/ds-router/install-manifest
# ============================================================================
set -eu

VERSION=1
PROG=install.sh

DRY_RUN=0
DO_SERVICE=1
DO_SYMLINK=1
ASSUME_YES=0

# ---------------------------------------------------------------------------
# output helpers — every action is echoed with a verb
# ---------------------------------------------------------------------------
say()  { printf '%s\n' "$*"; }
hdr()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
skip() { printf '  skip  %s\n' "$*"; }
note() { printf '  note  %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  # Print the leading comment block, stopping at the first non-comment line,
  # so editing the header cannot leak implementation lines into --help.
  sed -n '3,/^[^#]/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' \
    | sed '/^=\+$/d'
}

# ---------------------------------------------------------------------------
# args
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run)     DRY_RUN=1 ;;
    --no-service)  DO_SERVICE=0 ;;
    --no-symlink)  DO_SYMLINK=0 ;;
    -y|--yes)      ASSUME_YES=1 ;;
    -h|--help)     usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------
# Fail with something readable rather than a raw `set -u` message: these are
# the two environment values every path below is built from.
: "${HOME:?HOME is not set — run this from a normal login shell}"
command -v dirname >/dev/null 2>&1 || {
  printf 'ERROR: the standard shell tools (dirname, sed, cmp, cp, readlink) must be on PATH.\n' >&2
  exit 1
}

# Resolve this script's own directory, following symlinks, without readlink -f
# (which BSD readlink does not have).
self=$0
while [ -h "$self" ]; do
  link=$(readlink "$self")
  case $link in
    /*) self=$link ;;
    *)  self=$(dirname "$self")/$link ;;
  esac
done
# shellcheck disable=SC1007  # CDPATH= is a deliberate env override for cd,
# not an assignment typo: it stops a user's CDPATH from changing the result.
ROUTER_DIR=$(CDPATH= cd "$(dirname "$self")" && pwd -P)

CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
STATE_HOME=${XDG_STATE_HOME:-$HOME/.local/state}
SYSTEMD_USER_DIR=$CONFIG_HOME/systemd/user
STATE_DIR=$STATE_HOME/ds-router
MANIFEST=$STATE_DIR/install-manifest
BIN_DIR=$HOME/.local/bin
SYMLINK=$BIN_DIR/ds-switch

TMPDIR_D=${TMPDIR:-/tmp}
TMP_FILE=$TMPDIR_D/ds-router-install.$$.tmp
TMP_UNIT=$TMPDIR_D/ds-router-install.$$.unit
TMP_MANIFEST=$TMPDIR_D/ds-router-install.$$.manifest
cleanup() { rm -f "$TMP_FILE" "$TMP_UNIT" "$TMP_MANIFEST"; }
trap cleanup EXIT INT TERM HUP

# ---------------------------------------------------------------------------
# action wrappers — dry-run short-circuits every mutating operation
# ---------------------------------------------------------------------------
run_cmd() {
  # Perform a stateful command. Prints it either way.
  if [ "$DRY_RUN" = 1 ]; then
    printf '  dry   %s\n' "$*"
    return 0
  fi
  printf '  run   %s\n' "$*"
  "$@"
}

run_cmd_soft() {
  # Same, but a failure is a warning rather than a fatal error.
  #
  # Always returns 0. Returning nonzero here looks harmless but is not: under
  # `set -e` the status becomes the function's own, so a bare call at the call
  # site would kill the script right after printing "continuing" -- leaving a
  # half-install (units written, symlink and manifest missing) with no message
  # saying so. The warning is the report; the status is not a verdict.
  if [ "$DRY_RUN" = 1 ]; then
    printf '  dry   %s\n' "$*"
    return 0
  fi
  printf '  run   %s\n' "$*"
  if ! "$@"; then
    warn "command failed (continuing): $*"
  fi
  return 0
}

do_mkdir() {
  if [ -d "$1" ]; then
    skip "directory exists: $1"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf '  dry   mkdir -p %s\n' "$1"
    return 0
  fi
  mkdir -p "$1"
  printf '  mkdir %s\n' "$1"
}

# Escape a string for use as the replacement side of a sed s/// command.
escape_sed() { printf '%s' "$1" | sed 's/[&\\|]/\\&/g'; }

# ---------------------------------------------------------------------------
# 1. prerequisites
# ---------------------------------------------------------------------------
HERMES_BIN=""
PY=python3
PY_VERSION=""

check_python() {
  command -v python3 >/dev/null 2>&1 || die \
    "python3 is not on PATH. ds-router is Python 3.10+.
    Debian/Ubuntu : sudo apt install python3 python3-yaml
    Fedora        : sudo dnf install python3 python3-pyyaml
    Arch          : sudo pacman -S python python-yaml
    macOS         : brew install python   (or: python3 -m pip install --user pyyaml)"

  PY_VERSION=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")
  [ -n "$PY_VERSION" ] || die "python3 exists but could not be run: $PY"

  # 3.10 minimum: apply.py uses PEP 604 unions and builtin generics in
  # annotations, and router.py relies on the same syntax.
  if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    die "python3 is $PY_VERSION, but ds-router needs 3.10 or newer (PEP 604 unions).
    Debian/Ubuntu : sudo apt install python3.11
    Fedora        : sudo dnf install python3.12
    macOS         : brew install python@3.12"
  fi
  ok "python3 $PY_VERSION ($(command -v python3))"
}

check_hermes() {
  if command -v hermes >/dev/null 2>&1; then
    HERMES_BIN=$(command -v hermes)
    ok "hermes CLI: $HERMES_BIN"
    return 0
  fi
  die "the 'hermes' CLI is not on PATH. ds-router steers Hermes; without it
    there is nothing to steer.
    Install Hermes  : curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh
    Already installed? Its launcher is usually in ~/.local/bin, which is not
    always on PATH for non-login shells. Add to your shell profile:
        export PATH=\"\$HOME/.local/bin:\$PATH\"
    Then re-run this installer. (docs: https://hermes-agent.nousresearch.com/docs/)"
}

check_pyyaml() {
  if "$PY" -c 'import yaml' >/dev/null 2>&1; then
    ok "pyyaml importable"
    return 0
  fi

  warn "pyyaml is not importable by python3 — config.yaml cannot be read without it."
  if [ "$DRY_RUN" = 1 ]; then
    note "dry run: would offer to install pyyaml via 'python3 -m pip install --user pyyaml'"
    return 0
  fi

  answer=n
  if [ "$ASSUME_YES" = 1 ]; then
    answer=y
  elif [ -t 0 ]; then
    printf '  ask   install pyyaml now with pip --user? [y/N] '
    read -r answer 2>/dev/null || answer=n
  else
    note "not a terminal: assuming no (use --yes to install unattended)"
  fi

  case $answer in
    y|Y|yes|YES)
      if install_pyyaml; then
        ok "pyyaml importable"
        return 0
      fi
      warn "pip could not install pyyaml; falling back to manual instructions"
      ;;
    *) note "declined" ;;
  esac

  die "pyyaml is required. Install it with ONE of:
    Debian/Ubuntu : sudo apt install python3-yaml
    Fedora        : sudo dnf install python3-pyyaml
    Arch          : sudo pacman -S python-yaml
    macOS         : python3 -m pip install --user --break-system-packages pyyaml
    any platform  : python3 -m pip install --user pyyaml
                    (if that is refused as 'externally-managed-environment', add
                     --break-system-packages, or install your distro's python3-yaml)
    No pip at all : python3 -m venv \"\$HOME/.local/share/ds-router/venv\" &&
                    \"\$HOME/.local/share/ds-router/venv/bin/pip\" install pyyaml
                    (a venv only helps if its bin dir comes first on PATH —
                     the systemd unit sets PATH explicitly, so the distro package
                     or a --user install is preferred)
  Then re-run this installer."
}

install_pyyaml() {
  command -v python3 >/dev/null 2>&1 || return 1
  if ! "$PY" -m pip --version >/dev/null 2>&1; then
    warn "python3 -m pip is not available"
    return 1
  fi

  printf '  run   python3 -m pip install --user pyyaml\n'
  if "$PY" -m pip install --user pyyaml >"$TMP_FILE" 2>&1; then
    tail -n 2 "$TMP_FILE" | sed 's/^/        /'
    return 0
  fi

  # PEP 668 (Debian 12+, Fedora 38+, Homebrew python ≥3.12) refuses --user
  # installs with "externally-managed-environment". Retrying with the explicit
  # override is still user-scoped and still reversible via pip uninstall.
  if grep -q 'externally-managed-environment' "$TMP_FILE" 2>/dev/null; then
    warn "pip refused this as an externally managed environment (PEP 668); retrying with --break-system-packages"
    printf '  run   python3 -m pip install --user --break-system-packages pyyaml\n'
    if "$PY" -m pip install --user --break-system-packages pyyaml >"$TMP_FILE" 2>&1; then
      tail -n 2 "$TMP_FILE" | sed 's/^/        /'
      return 0
    fi
  fi

  printf '  fail  pip said (last lines):\n'
  tail -n 5 "$TMP_FILE" | sed 's/^/        /' >&2
  return 1
}

hdr "Prerequisites"
check_python
check_hermes
check_pyyaml

# ---------------------------------------------------------------------------
# 2. sanity: is this actually the ds-router tree?
# ---------------------------------------------------------------------------
hdr "Checking the source tree"
for f in ds-switch apply.py router.py config.yaml; do
  [ -f "$ROUTER_DIR/$f" ] || die "$f not found in $ROUTER_DIR — run install.sh from the ds-router checkout."
done
ok "found ds-switch, apply.py, router.py, config.yaml in $ROUTER_DIR"

if [ -x "$ROUTER_DIR/ds-switch" ]; then
  skip "ds-switch is already executable"
elif [ "$DRY_RUN" = 1 ]; then
  printf '  dry   chmod +x %s\n' "$ROUTER_DIR/ds-switch"
else
  chmod +x "$ROUTER_DIR/ds-switch"
  printf '  chmod +x %s\n' "$ROUTER_DIR/ds-switch"
fi

# ---------------------------------------------------------------------------
# 3. preflight: the test suites must pass before anything is installed
# ---------------------------------------------------------------------------
hdr "Preflight: test suites"
tests_run=0
for t in "$ROUTER_DIR"/test_*.py; do
  [ -f "$t" ] || continue
  tests_run=$((tests_run + 1))
  name=$(basename "$t")
  if out=$(cd "$ROUTER_DIR" && "$PY" "$t" 2>&1); then
    last=$(printf '%s\n' "$out" | tail -n 1)
    ok "$name passed ($last)"
  else
    printf '%s\n' "$out" | tail -n 20 | sed 's/^/        /' >&2
    die "$name FAILED — refusing to install a broken tree.
    Fix the failure above (or run: cd $ROUTER_DIR && python3 $name), then re-run."
  fi
done
if [ "$tests_run" = 0 ]; then
  warn "no test_*.py files found in $ROUTER_DIR — preflight skipped"
else
  ok "$tests_run test suite(s) passed"
fi

# ---------------------------------------------------------------------------
# 4. systemd units (Linux) / launchd + cron instructions (macOS)
# ---------------------------------------------------------------------------
# The checked-in units are written for a checkout at %h/Projects/ds-router with
# Hermes in %h/.hermes/. Both are rendered to this machine's real paths on the
# way in, so the units work from any clone location — and uninstall.sh only
# deletes a unit that matches exactly what this installer would write.
render_unit() {
  # $1 = template path; writes the rendered unit to stdout.
  hermes_bin_dir=$(dirname "$HERMES_BIN")
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
      "$1"
}

install_units_linux() {
  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemd (systemctl) is not available on this system."
    cron_hint
    return 0
  fi
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    warn "no reachable systemd --user session (are you in a login session?)."
    note "the units were still installed; start them later with:"
    note "  systemctl --user daemon-reload && systemctl --user enable --now ds-router.timer"
    note "for a timer that survives logout, also run: loginctl enable-linger ${USER:-$(id -un)}"
    do_mkdir "$SYSTEMD_USER_DIR"
  else
    do_mkdir "$SYSTEMD_USER_DIR"
  fi

  changed=0
  for src in "$ROUTER_DIR"/systemd/*.service "$ROUTER_DIR"/systemd/*.timer; do
    [ -f "$src" ] || continue
    base=$(basename "$src")
    dst=$SYSTEMD_USER_DIR/$base
    render_unit "$src" >"$TMP_UNIT"
    if [ -f "$dst" ] && cmp -s "$TMP_UNIT" "$dst"; then
      skip "unit already current: $dst"
      continue
    fi
    if [ "$DRY_RUN" = 1 ]; then
      printf '  dry   install %s -> %s\n' "$src" "$dst"
      changed=1
      continue
    fi
    cp "$TMP_UNIT" "$dst"
    printf '  write %s\n' "$dst"
    changed=1
  done

  if [ "$changed" = 1 ] || [ ! -f "$SYSTEMD_USER_DIR/ds-router.timer" ]; then
    run_cmd_soft systemctl --user daemon-reload
  else
    skip "systemd daemon-reload not needed (no unit changed)"
  fi

  if [ -f "$SYSTEMD_USER_DIR/ds-router.timer" ] || [ "$DRY_RUN" = 1 ]; then
    run_cmd_soft systemctl --user enable --now ds-router.timer
    # Verify rather than assume: systemctl can fail against a bus that is not
    # reachable, and a silently un-enabled timer is a half-install.
    if ! systemctl --user is-active ds-router.timer >/dev/null 2>&1; then
      warn "the timer is not active; start it with:"
      note "  systemctl --user daemon-reload && systemctl --user enable --now ds-router.timer"
    fi
  fi
  note "check it with: systemctl --user list-timers ds-router.timer"
}

cron_hint() {
  note "no timer installed. Equivalent cron line (every 15 min, offset by 4):"
  note "  4,19,34,49 * * * * cd $ROUTER_DIR && ./ds-switch >> ${STATE_DIR}/cron.log 2>&1"
}

launchd_plist_dst() { printf '%s\n' "$HOME/Library/LaunchAgents/com.ds-router.switch.plist"; }

install_launchd_darwin() {
  dst=$(launchd_plist_dst)
  plist_dir=$(dirname "$dst")
  do_mkdir "$plist_dir"

  hermes_bin_dir=$(dirname "$HERMES_BIN")
  run_path="$hermes_bin_dir:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

  if [ "$DRY_RUN" = 1 ]; then
    printf '  dry   write %s\n' "$dst"
  else
    cat >"$TMP_UNIT" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ds-router.switch</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROUTER_DIR/ds-switch</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>900</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$run_path</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$STATE_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>$STATE_DIR/launchd.err.log</string>
</dict>
</plist>
PLIST_EOF
    if [ -f "$dst" ] && cmp -s "$TMP_UNIT" "$dst"; then
      skip "plist already current: $dst"
    else
      cp "$TMP_UNIT" "$dst"
      printf '  write %s\n' "$dst"
    fi
  fi

  say ""
  say "  systemd is Linux-only, so on macOS the equivalent is a launchd agent."
  say "  The plist above was written but deliberately NOT loaded: loading it needs"
  say "  your GUI login session, which an installer cannot assume. Load it with:"
  say ""
  say "      launchctl bootstrap gui/\$(id -u) \"$dst\"     # modern macOS"
  say "      launchctl load -w \"$dst\"                     # older macOS"
  say ""
  say "  Or use cron instead (crontab -e), every 15 minutes:"
  say ""
  say "      4,19,34,49 * * * * cd $ROUTER_DIR && ./ds-switch >> $STATE_DIR/cron.log 2>&1"
  say ""
  say "  Undo later with: launchctl bootout gui/\$(id -u) \"$dst\" && ./uninstall.sh"
}

hdr "Service / timer"
if [ "$DO_SERVICE" = 0 ]; then
  skip "--no-service: printing what would have been installed, changing nothing"
  note "systemd units (Linux): $SYSTEMD_USER_DIR/ds-router.{service,timer}"
  note "launchd plist (macOS): $HOME/Library/LaunchAgents/com.ds-router.switch.plist"
  cron_hint
else
  case $(uname -s) in
    Linux)  install_units_linux ;;
    Darwin) install_launchd_darwin ;;
    *)
      warn "unrecognised platform: $(uname -s) — no service installed"
      cron_hint
      ;;
  esac
fi

# ---------------------------------------------------------------------------
# 5. optional PATH symlink
# ---------------------------------------------------------------------------
hdr "Command-line convenience"
if [ "$DO_SYMLINK" = 0 ]; then
  skip "--no-symlink: leaving $SYMLINK alone"
else
  do_mkdir "$BIN_DIR"
  if [ -L "$SYMLINK" ]; then
    existing=$(readlink "$SYMLINK")
    if [ "$existing" = "$ROUTER_DIR/ds-switch" ]; then
      skip "symlink already correct: $SYMLINK -> $existing"
    elif [ "$DRY_RUN" = 1 ]; then
      printf '  dry   ln -sf %s %s (replacing -> %s)\n' "$ROUTER_DIR/ds-switch" "$SYMLINK" "$existing"
    else
      ln -sf "$ROUTER_DIR/ds-switch" "$SYMLINK"
      printf '  link  %s -> %s (replaced -> %s)\n' "$SYMLINK" "$ROUTER_DIR/ds-switch" "$existing"
    fi
  elif [ -e "$SYMLINK" ]; then
    warn "$SYMLINK exists and is not a symlink — leaving it alone"
    warn "remove it yourself, or use $ROUTER_DIR/ds-switch directly"
  elif [ "$DRY_RUN" = 1 ]; then
    printf '  dry   ln -s %s %s\n' "$ROUTER_DIR/ds-switch" "$SYMLINK"
  else
    ln -s "$ROUTER_DIR/ds-switch" "$SYMLINK"
    printf '  link  %s -> %s\n' "$SYMLINK" "$ROUTER_DIR/ds-switch"
  fi
  case ":${PATH:-}:" in
    *":$BIN_DIR:"*) skip "$BIN_DIR is already on PATH" ;;
    *) warn "$BIN_DIR is not on PATH in this shell; add:
        export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac
fi
note "ds-switch always works as $ROUTER_DIR/ds-switch regardless"

# ---------------------------------------------------------------------------
# 6. verify
# ---------------------------------------------------------------------------
hdr "Verifying the install"
if [ -x "$ROUTER_DIR/ds-switch" ] || [ "$DRY_RUN" = 1 ]; then
  printf '  run   %s --check\n' "$ROUTER_DIR/ds-switch"
  check_rc=0
  check_out=$(cd "$ROUTER_DIR" && "$ROUTER_DIR/ds-switch" --check 2>&1) || check_rc=$?
  printf '%s\n' "$check_out" | sed 's/^/        /'
  if [ "$check_rc" = 0 ]; then
    ok "ds-switch --check passed: the config can drive Hermes"
  else
    warn "ds-switch --check reported problems (exit $check_rc) — the files are installed,"
    warn "but Hermes will not be steered correctly until the errors above are fixed."
  fi
else
  warn "ds-switch is not executable; skipping verification"
fi

# ---------------------------------------------------------------------------
# 7. manifest — the record uninstall.sh reverses
# ---------------------------------------------------------------------------
hdr "Recording what was installed"
if [ "$DRY_RUN" = 1 ]; then
  printf '  dry   write %s\n' "$MANIFEST"
else
  do_mkdir "$STATE_DIR"
  {
    # Raw values: uninstall.sh parses this file as data (never sources it), so a
    # path containing a space or a shell metacharacter survives intact.
    printf 'VERSION=%s\n' "$VERSION"
    printf 'PROGRAM=%s\n' "$PROG"
    printf 'ROUTER_DIR=%s\n' "$ROUTER_DIR"
    printf 'PLATFORM=%s\n' "$(uname -s)"
    printf 'SERVICE=%s\n' "$DO_SERVICE"
    printf 'SYMLINK_ENABLED=%s\n' "$DO_SYMLINK"
    printf 'SYMLINK=%s\n' "$SYMLINK"
    printf 'SYSTEMD_USER_DIR=%s\n' "$SYSTEMD_USER_DIR"
    printf 'LAUNCHD_PLIST=%s\n' "$(launchd_plist_dst)"
    printf 'HERMES_BIN=%s\n' "$HERMES_BIN"
  } >"$TMP_MANIFEST"
  # Rewriting an identical manifest would be a no-op write; keep re-runs clean.
  if [ -f "$MANIFEST" ] && cmp -s "$TMP_MANIFEST" "$MANIFEST"; then
    skip "manifest already current: $MANIFEST"
    rm -f "$TMP_MANIFEST"
  else
    mv "$TMP_MANIFEST" "$MANIFEST"
    printf '  write %s\n' "$MANIFEST"
  fi
fi

# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------
hdr "Installed"
say "  project   : $ROUTER_DIR"
say "  try it    : cd $ROUTER_DIR && ./ds-switch --show     # decide, write nothing"
say "              cd $ROUTER_DIR && ./ds-switch            # apply the decision"
say "  verify    : ./ds-switch --check"
say "  uninstall : $ROUTER_DIR/uninstall.sh --dry-run       # then without --dry-run"
if [ "$DRY_RUN" = 1 ]; then
  say ""
  say "  DRY RUN — nothing above was written or changed."
fi
say ""
