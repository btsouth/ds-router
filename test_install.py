#!/usr/bin/env python3
"""Tests for install.sh: the one component that writes outside the repo.

It had no automated coverage at all, and the reason was real: install.sh runs every
test suite as its preflight, so a suite that runs install.sh would run itself. The
`--skip-preflight` flag exists for this file (and for a re-run right after you ran the
suites yourself).

The first test below is not a formality. The usage text is rendered by stripping `#`
from the leading comment block, so a single un-commented line in that block becomes a
command the shell executes on startup. That happened: a help line lost its `#` and the
installer re-ran itself with its own help text as arguments, forking until the box
refused. `sh -n` passes a line like that, and so does `shellcheck`, because it is
valid shell. Running `--help` and noticing it never returns is what catches it.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import testkit

INSTALL = HERE / "install.sh"

# install.sh installs a systemd user timer on Linux and a launchd plist on macOS, and
# deliberately does not load the plist. CI runs both, so the platform-specific
# assertions below are gated rather than assumed: on a platform where a check cannot
# run, it says so instead of passing quietly.
LINUX = sys.platform.startswith("linux")
DARWIN = sys.platform == "darwin"


def skip_note(what: str) -> None:
    print(f"  note  {what} (not applicable on {sys.platform})")


def check(label: str, ok: bool, detail: str = "") -> None:
    """A named assertion: raise, so the runner reports the test, the label and the line."""
    if not ok:
        raise AssertionError(label + (f": {detail}" if detail else ""))


def fake_systemctl(tmp: pathlib.Path, *, bus: bool) -> pathlib.Path:
    """A `systemctl` stand-in, so these tests never touch the real user manager.

    With `bus=False` it answers the way a box with no reachable `systemd --user`
    session does: `show-environment` fails, so the installer cannot enable anything.
    """
    binary = tmp / "systemctl"
    binary.write_text(
        "#!/bin/sh\n"
        f'BUS={1 if bus else 0}\n'
        'for arg in "$@"; do\n'
        '  case $arg in\n'
        '    show-environment) [ "$BUS" = 1 ] || exit 1 ; exit 0 ;;\n'
        '    is-active) [ "$BUS" = 1 ] || exit 3 ; echo active ; exit 0 ;;\n'
        '  esac\n'
        'done\n'
        'exit 0\n')
    binary.chmod(0o755)
    return binary


def fake_hermes(tmp: pathlib.Path) -> pathlib.Path:
    """A `hermes` stand-in, so this suite needs no Hermes install and no real config.

    install.sh refuses to run without the CLI on PATH, and `ds-switch --check` reads
    the config through it. The notice below is the one Hermes prints for a key that is
    not set, which apply.py must treat as an answer rather than a failed read.
    """
    binary = tmp / "hermes"
    binary.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"config get"*) echo "Config key not set: $3" >&2 ; exit 1 ;;\n'
        'esac\n'
        'exit 0\n')
    binary.chmod(0o755)
    return binary


def sandbox_env(tmp: pathlib.Path, home: pathlib.Path) -> dict[str, str]:
    """An environment with the fakes first and nothing of the caller's.

    Built rather than inherited, because an exported HERMES_HOME or a real provider key
    in the calling shell would let a sandbox test pass while the same code fails on a
    stranger's machine. PATH is the one exception, and it is prepended rather than
    replaced: the fakes have to win, and everything after them has to keep working.
    `ds-switch` execs `python3`, and on macOS there is no python3 in `/usr/bin`, so a
    PATH built from system directories alone left the installer unable to verify its own
    install. `sys.executable`'s directory is the interpreter running this suite, which
    is the one python3 that is certainly present.
    """
    return {
        "PATH": os.pathsep.join([str(tmp), os.path.dirname(sys.executable),
                                 os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")]),
        "HOME": str(home),
        "HERMES_HOME": str(home / ".hermes"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".state"),
        "XDG_RUNTIME_DIR": str(tmp),
        "LC_ALL": "C",
        "TMPDIR": str(tmp),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def run_install(tmp: pathlib.Path, *args: str, bus: bool = True,
                keys: dict[str, str] | None = None,
                timeout: int = 120) -> subprocess.CompletedProcess:
    """Run the installer against a throwaway home, with the fakes on PATH."""
    fake_systemctl(tmp, bus=bus)
    fake_hermes(tmp)
    home = tmp / "home"
    (home / ".hermes").mkdir(parents=True, exist_ok=True)
    env = sandbox_env(tmp, home)
    if keys:
        env.update(keys)
    return subprocess.run([str(INSTALL), *args], capture_output=True, text=True,
                          env=env, cwd=str(HERE), timeout=timeout)


def test_help_returns_and_prints_its_header(tmp: pathlib.Path) -> None:
    """A stray un-commented line in the header block makes the installer run itself."""
    try:
        proc = run_install(tmp, "--help", timeout=30)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "install.sh --help did not return: a line in the header comment block is "
            "executing, which re-runs the installer with the help text as arguments")
    check("--help exits 0", proc.returncode == 0, str(proc.returncode))
    check("the header is printed", "./install.sh --dry-run" in proc.stdout, proc.stdout[:200])
    check("the new flag is documented", "--skip-preflight" in proc.stdout)
    check("nothing recursed", "shell level" not in proc.stderr, proc.stderr[:200])
    check("no argument was misread", "unknown argument" not in proc.stderr, proc.stderr[:200])


def test_a_dry_run_writes_nothing_and_reports_the_timer_state(tmp: pathlib.Path) -> None:
    """The banner has to carry the timer's state, not just the word Installed."""
    proc = run_install(tmp, "--dry-run", "--skip-preflight", "--yes", bus=False)
    check("the dry run exits 0", proc.returncode == 0, proc.stderr[-300:])
    written = [p.name for p in (tmp / "home").rglob("*") if p.is_file()
               and ".hermes" not in str(p)]
    check("nothing was written", not written, str(written))
    check("the banner carries the timer state", "timer     : " in proc.stdout,
          proc.stdout[-400:])
    if not LINUX:
        skip_note("the systemd assertions in this test")
        return
    check("the banner says the timer is not active",
          "NOT active (no reachable systemd --user session)" in proc.stdout,
          proc.stdout[-400:])
    check("it says the units were still installed",
          "the units were still installed" in proc.stdout, proc.stdout[-400:])

    # The note used to be printed before the writes it describes, which reads as a
    # contradiction: say the units are installed before saying you install them.
    install_line = next(i for i, line in enumerate(proc.stdout.splitlines())
                        if "ds-router.timer" in line and "dry" in line)
    note_line = next(i for i, line in enumerate(proc.stdout.splitlines())
                     if "the units were still installed" in line)
    check("the note comes after the write it describes", note_line > install_line,
          f"install at {install_line}, note at {note_line}")


def test_a_reachable_bus_reports_the_timer_active(tmp: pathlib.Path) -> None:
    if not LINUX:
        skip_note("this test: there is no systemd bus to reach")
        return
    proc = run_install(tmp, "--dry-run", "--skip-preflight", "--yes", bus=True)
    check("the dry run exits 0", proc.returncode == 0, proc.stderr[-300:])
    check("the banner says the timer is active", "timer     : active" in proc.stdout,
          proc.stdout[-400:])
    check("and it does not warn about a missing session",
          "the units were still installed" not in proc.stdout, proc.stdout[-400:])


def test_an_uninstall_leaves_no_empty_directories_behind(tmp: pathlib.Path) -> None:
    """Uninstall has to remove what it installed, and the directories it made for it.

    The units' directory was left behind empty, which is a trace of a tool the user has
    just asked to remove. `rmdir` cannot take anyone else's unit with it: it only
    succeeds on an empty directory.
    """
    home = tmp / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    plist = home / "Library" / "LaunchAgents" / "com.ds-router.switch.plist"
    installed = plist if DARWIN else unit_dir / "ds-router.timer"
    proc = run_install(tmp, "--skip-preflight", "--yes")
    check("the install exits 0", proc.returncode == 0, proc.stderr[-300:])
    check("the service file was installed", installed.is_file(), str(installed))

    uninstall = subprocess.run([str(HERE / "uninstall.sh"), "--yes"], capture_output=True,
                              text=True, env=sandbox_env(tmp, home),
                              cwd=str(HERE), timeout=120)
    check("the uninstall exits 0", uninstall.returncode == 0, uninstall.stderr[-300:])
    check("the service file is gone", not installed.exists(), str(installed))
    if LINUX:
        check("the empty unit directory is gone too", not unit_dir.exists(), str(unit_dir))
    else:
        skip_note("the units-directory assertion in this test")
    check("the manifest is gone",
          not (home / ".state" / "ds-router" / "install-manifest").exists())


def test_the_installer_repeats_the_check_summary_rather_than_its_own(
        tmp: pathlib.Path) -> None:
    """`config OK` and `no provider key is set` are different statements.

    The installer used to print "the config can drive Hermes" over the check's own
    output saying no key was set, which is the sentence a reader takes away.
    """
    proc = run_install(tmp, "--dry-run", "--skip-preflight", "--yes")
    check("the dry run exits 0", proc.returncode == 0, proc.stderr[-300:])
    passed = [line for line in proc.stdout.splitlines() if "--check passed" in line]
    check("the check ran", bool(passed), proc.stdout[-400:])
    check("the banner repeats the real summary, not a nicer one",
          "no provider key is set" in passed[0], passed[0])


def test_the_installed_unit_puts_the_verified_python3_on_path(tmp: pathlib.Path) -> None:
    """The unit hardcoded /usr/bin:/usr/local/bin, so a pyenv/asdf/mise/Homebrew
    interpreter was invisible to the timer even though the install succeeded: the
    service then failed every 15 minutes with 'python3: not found'. The rendered unit
    must carry the directory of the python3 the installer actually verified."""
    proc = run_install(tmp, "--skip-preflight", "--yes")
    check("the install exits 0", proc.returncode == 0, proc.stderr[-300:])
    home = tmp / "home"
    unit = (home / "Library" / "LaunchAgents" / "com.ds-router.switch.plist" if DARWIN
            else home / ".config" / "systemd" / "user" / "ds-router.service")
    check("the unit was installed", unit.is_file(), str(unit))
    if not unit.is_file():
        return
    text = unit.read_text()
    expected = os.path.dirname(sys.executable)
    check("no unsubstituted token is left behind", "%python3_bin_dir%" not in text, text)
    check("the verified python3 directory is on the unit's PATH", expected in text,
          f"{expected!r} not found in unit: {text}")


def test_the_uninstaller_still_matches_units_when_python3_has_moved(tmp: pathlib.Path) -> None:
    """The uninstaller re-renders the units it expects to find, and the renderer
    has to use the SAME python3 directory the units were written with. Re-deriving
    that directory from the current shell read a freshly installed unit as
    hand-edited the moment the interpreter moved (a removed pyenv, an upgrade), and
    the uninstaller then refused without --force. install.sh records the directory
    in the manifest, and the uninstaller trusts that record."""
    proc = run_install(tmp, "--skip-preflight", "--yes")
    check("the install exits 0", proc.returncode == 0, proc.stderr[-300:])
    manifest = tmp / "home" / ".state" / "ds-router" / "install-manifest"
    check("the manifest exists", manifest.is_file(), str(manifest))
    expected = os.path.dirname(sys.executable)
    check("the manifest records the verified python3 directory",
          f"PYTHON3_BIN_DIR={expected}" in manifest.read_text(), manifest.read_text())
    if not LINUX:
        skip_note("the systemd unit assertions in this test")
        return
    unit_dir = tmp / "home" / ".config" / "systemd" / "user"
    unit = unit_dir / "ds-router.service"
    check("the unit was installed", unit.is_file(), str(unit))

    # A DIFFERENT python3 first on PATH, so `command -v python3` answers somewhere
    # else than the interpreter the install verified.
    altbin = tmp / "altbin"
    altbin.mkdir(exist_ok=True)
    alt_python = altbin / "python3"
    alt_python.write_text("#!/bin/sh\nexit 0\n")
    alt_python.chmod(0o755)
    env = sandbox_env(tmp, tmp / "home")
    env["PATH"] = os.pathsep.join([str(altbin), env["PATH"]])
    uninstall = subprocess.run([str(HERE / "uninstall.sh"), "--yes"], capture_output=True,
                               text=True, env=env, cwd=str(HERE), timeout=120)
    check("the uninstall exits 0", uninstall.returncode == 0, uninstall.stderr[-300:])
    check("the unit is gone", not unit.exists(), str(unit))
    transcript = uninstall.stdout + uninstall.stderr
    check("the uninstaller did not read its own unit as hand-edited",
          "does not match" not in transcript, transcript[-400:])


if __name__ == "__main__":
    raise SystemExit(testkit.run(globals(), scratch_prefix="ds-install-"))
