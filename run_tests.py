#!/usr/bin/env python3
"""Run every test suite in this directory.

Each suite is a plain script with its own __main__ runner, so this just finds
them and aggregates. Exits nonzero if any suite fails.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def suites() -> list[Path]:
    return sorted(p for p in HERE.glob("test_*.py") if p.is_file())


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
        total_pass += passes
        total_fail += fails
        if proc.returncode != 0:
            failed_suites += 1
        status = "ok  " if proc.returncode == 0 else "FAIL"
        print(f"  {status} {path.name:28} {passes:>3} passed, {fails} failed")
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
