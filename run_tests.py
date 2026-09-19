#!/usr/bin/env python3
"""Run every test suite in this directory.

Each suite is a plain script with its own __main__ runner, so this just finds
them and aggregates. Exits nonzero if any suite fails.
"""

from __future__ import annotations

import os
import tempfile
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent


def suites() -> list[Path]:
    return sorted(p for p in HERE.glob("test_*.py") if p.is_file())


def _declared_total(out: str) -> Optional[int]:
    """The count from a suite's own "N passed, M failed" summary, if present."""
    for line in reversed(out.splitlines()):
        match = re.match(r"\s*(\d+) passed,\s*(\d+) failed\s*$", line)
        if match:
            return int(match.group(1))
    return None


def main() -> int:
    # One disposable parent also catches scratch directories left by older suites.
    with tempfile.TemporaryDirectory(prefix="ds-router-tests-") as tmp:
        env = dict(os.environ, TMPDIR=tmp, HOME=tmp, HERMES_HOME=str(Path(tmp) / "hermes"),
                   XDG_STATE_HOME=str(Path(tmp) / "state"),
                   XDG_CONFIG_HOME=str(Path(tmp) / "config"), PYTHONDONTWRITEBYTECODE="1")
        for key in ("COMMANDCODE_API_KEY", "OPENCODE_GO_API_KEY", "OLLAMA_API_KEY",
                    "HERMES_CUSTOM_CLINEPASS_API_KEY"):
            env.pop(key, None)
        return run_suites(env)


def run_suites(env) -> int:
    found = suites()
    if not found:
        print("no test suites found", file=sys.stderr)
        return 1

    total_pass = total_fail = failed_suites = 0
    for path in found:
        proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, cwd=HERE, env=env)
        out = proc.stdout or ""
        passes = sum(1 for line in out.splitlines() if line.startswith("  pass"))
        fails = sum(1 for line in out.splitlines() if line.startswith("  FAIL"))
        # A suite whose own summary disagrees with the counted assertions is
        # reporting its results in a way this runner cannot read, which would
        # otherwise show up as "0 passed" on a green run. Treat that as a failure
        # rather than trusting the exit code.
        declared = _declared_total(out)
        miscounted = declared is not None and declared != passes and proc.returncode == 0
        # A suite that ran nothing is not a passing suite. Catching this is the
        # whole reason this runner exists: a renamed or silently-skipped test used
        # to vanish from the count while the run stayed green.
        empty = passes == 0 and fails == 0 and proc.returncode == 0
        if miscounted or empty:
            passes, fails = passes, fails + 1
        total_pass += passes
        total_fail += fails
        if proc.returncode != 0 or miscounted or empty:
            failed_suites += 1
        status = "ok  " if proc.returncode == 0 and not miscounted and not empty else "FAIL"
        print(f"  {status} {path.name:28} {passes:>3} passed, {fails} failed")
        if miscounted:
            print(f"        suite declared {declared} passed but printed {passes} "
                  f"'  pass' lines; its output format is unreadable to this runner")
        if empty:
            print(f"        suite reported no tests at all (exit 0). Either it ran "
                  f"nothing, or its result lines do not start with '  pass'.")
        if proc.returncode != 0:
            for line in out.splitlines():
                if line.startswith("  FAIL"):
                    print(f"        {line.strip()}")
            if proc.stderr.strip():
                print(f"        stderr: {proc.stderr.strip()[:300]}")

    print()
    summary = f"{len(found)} suite(s), {total_pass} tests passed, {total_fail} failed"
    if failed_suites:
        # Named explicitly: a crashing suite contributes no `  FAIL` line, so
        # "0 failed" next to a FAIL row reads as a clean run.
        summary += f", {failed_suites} suite(s) did not report cleanly"
    print(summary)
    return 1 if (total_fail or failed_suites) else 0


if __name__ == "__main__":
    raise SystemExit(main())
