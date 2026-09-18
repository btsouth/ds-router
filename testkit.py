"""Shared pieces for the test suites.

The suites are plain scripts, deliberately: no pytest, no plugins, nothing to
install. This is the one bit of shared code, and it exists because of a specific
failure mode: a bare `assert x == y` with no message used to print

    FAIL  test_something:

with nothing after the colon, so the only way to find the failing line was to
re-run the suite by hand. A failure now names the file and line, and a suite that
raises something other than AssertionError reports it and carries on instead of
aborting every test after it.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


def _location(exc: BaseException) -> str:
    """`file:line` of the last frame in the traceback, or '' if there is none."""
    tb = exc.__traceback__
    last = None
    while tb is not None:
        last = tb
        tb = tb.tb_next
    if last is None:
        return ""
    return f"{last.tb_frame.f_code.co_filename}:{last.tb_lineno}"


def describe(exc: BaseException) -> str:
    """A one-line description of a failure that always says something useful."""
    message = str(exc).strip()
    where = _location(exc)
    if message and where:
        return f"{message}  [{where}]"
    if message:
        return message
    return f"{type(exc).__name__} raised at {where}" if where else type(exc).__name__


def run(namespace: dict[str, Any], *, scratch_prefix: str = "ds-test-") -> int:
    """Run every ``test_*`` callable in *namespace* and return a shell exit code.

    A test that takes one positional argument gets a fresh scratch directory, so a
    suite does not have to build its own temp tree. A test that raises is reported
    and the rest still run: one broken test used to abort every test after it.
    """
    tests: list[tuple[str, Callable[..., Any]]] = [
        (name, fn) for name, fn in sorted(namespace.items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        scratch = Path(tempfile.mkdtemp(prefix=f"{scratch_prefix}{name[:32]}-"))
        try:
            if fn.__code__.co_argcount:
                fn(scratch)
            else:
                fn()
        except Exception as exc:  # noqa: BLE001 - report it, never abort the suite
            failures += 1
            print(f"  FAIL  {name}: {describe(exc)}")
        else:
            print(f"  pass  {name}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    if not tests:
        # A suite that collects nothing is a broken suite, not a passing one.
        print("  FAIL  no test_* functions were collected", file=sys.stderr)
        failures += 1
    print(f"\n{len(tests) - failures} passed, {failures} failed")
    return 1 if failures else 0
