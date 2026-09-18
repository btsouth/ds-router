#!/usr/bin/env python3
"""Run every test suite in this directory.

Each suite is a plain script with its own __main__ runner, so this just finds
them and aggregates. Exits nonzero if any suite fails.
"""

from __future__ import annotations

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
    found = suites()
    if not found:
        print("no test suites found", file=sys.stderr)
        return 1

    total_pass = total_fail = failed_suites = 0
    for path in found:
        proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, cwd=HERE)
        out = proc.stdout or ""
        passes = sum(1 for line in out.splitlines() if line.startswith("  pass"))
        fails = sum(1 for line in out.splitlines() if line.startswith("  FAIL"))
        # A suite whose own summary disagrees with the counted assertions is
        # reporting its results in a way this runner cannot read, which would
        # otherwise show up as "0 passed" on a green run. Treat that as a failure
        # rather than trusting the exit code.
        declared = _declared_total(out)
        miscounted = declared is not None and declared != passes and proc.returncode == 0
        if miscounted:
            passes, fails = passes, fails + 1
        total_pass += passes
        total_fail += fails
        if proc.returncode != 0 or miscounted:
            failed_suites += 1
        status = "ok  " if proc.returncode == 0 and not miscounted else "FAIL"
        print(f"  {status} {path.name:28} {passes:>3} passed, {fails} failed")
        if miscounted:
            print(f"        suite declared {declared} passed but printed {passes} "
                  f"'  pass' lines; its output format is unreadable to this runner")
        if proc.returncode != 0:
            for line in out.splitlines():
                if line.startswith("  FAIL"):
                    print(f"        {line.strip()}")
            if proc.stderr.strip():
                print(f"        stderr: {proc.stderr.strip()[:300]}")

    print()
    print(f"{len(found)} suite(s), {total_pass} tests passed, {total_fail} failed")
    return 1 if (total_fail or failed_suites) else 0


if __name__ == "__main__":
    raise SystemExit(main())
