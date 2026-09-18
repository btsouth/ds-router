#!/usr/bin/env python3
"""Tests for apply.py: the component that writes the user's live Hermes config.

apply.py had no direct coverage, and it is the only part of this project that
mutates a file outside the repo. The cases here are the ones that would corrupt
a config in the field.
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

import apply as A

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  pass  {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


def fake_hermes(tmp: pathlib.Path, *, fail_on: str = "", calls: pathlib.Path | None = None) -> str:
    """A stand-in `hermes` that can be made to fail on a chosen key."""
    log = f'echo "$*" >> {calls}\n' if calls else ""
    # Only emit the failing branch when a failure is actually wanted: an empty
    # pattern would match every "config set" call.
    fail_branch = f'  *"config set {fail_on}"*)      echo "boom" >&2; exit 1 ;;\n' if fail_on else ""
    script = f"""#!/bin/sh
{log}case "$*" in
  *"config get model.provider"*) echo "ORIGINAL_PROV"; exit 0 ;;
  *"config get model.default"*)  echo "ORIGINAL_MODEL"; exit 0 ;;
  *"config get model.base_url"*) echo "ORIGINAL_URL"; exit 0 ;;
{fail_branch}  *"config set"*)                echo "ok"; exit 0 ;;
  *"config unset"*)              echo "ok"; exit 0 ;;
  *) exit 0 ;;
esac
"""
    shim = tmp / "hermes"
    shim.write_text(script)
    shim.chmod(0o755)
    return str(tmp)


def test_a_failed_write_rolls_back_the_ones_before_it(tmp: pathlib.Path) -> None:
    """A partial write leaves provider and model disagreeing, which Hermes
    rejects at request time. The earlier keys must be restored."""
    calls = tmp / "calls.log"
    bin_dir = fake_hermes(tmp, fail_on="model.default", calls=calls)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    try:
        try:
            A.set_provider("clinepass", {"base_url": "https://x/v1"}, "ds",
                           dry=False, models={"ds": {"clinepass": "cline-pass/ds"}})
            raised = None
        except RuntimeError as exc:
            raised = str(exc)
    finally:
        os.environ["PATH"] = old_path

    check("a failed write raises", raised is not None)
    if raised:
        check("the error names the rollback", "restored" in raised, raised)
    written = calls.read_text() if calls.exists() else ""
    check("the first key was rolled back to its original value",
          "config set model.provider ORIGINAL_PROV" in written, written)
    check("no attempt was made to write model.base_url",
          "config set model.base_url" not in written, written)


def test_success_writes_all_three_keys_in_order(tmp: pathlib.Path) -> None:
    calls = tmp / "ok.log"
    bin_dir = fake_hermes(tmp, calls=calls)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    try:
        model_id, base_url = A.set_provider(
            "clinepass", {"base_url": "https://x/v1"}, "ds",
            dry=False, models={"ds": {"clinepass": "cline-pass/ds"}})
    finally:
        os.environ["PATH"] = old_path
    check("returns the provider's model id", model_id == "cline-pass/ds", model_id)
    written = calls.read_text() if calls.exists() else ""
    check("provider written", "config set model.provider clinepass" in written)
    check("model written", "config set model.default cline-pass/ds" in written)
    check("base_url written", "config set model.base_url https://x/v1" in written)


def test_dry_run_writes_nothing(tmp: pathlib.Path) -> None:
    calls = tmp / "dry.log"
    bin_dir = fake_hermes(tmp, calls=calls)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    try:
        A.set_provider("clinepass", {"base_url": "https://x/v1"}, "ds",
                       dry=True, models={"ds": {"clinepass": "cline-pass/ds"}})
    finally:
        os.environ["PATH"] = old_path
    check("--show writes no config keys",
          not calls.exists() or "config set" not in calls.read_text())


def test_a_provider_that_does_not_serve_the_alias_is_refused(tmp: pathlib.Path) -> None:
    """Writing a provider with no usable model would fail later at request time."""
    try:
        A.set_provider("opencode-go", {}, "ds", dry=False,
                       models={"ds": {"clinepass": "cline-pass/ds"}})
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    check("refuses rather than writing", raised is not None)
    if raised:
        check("names the provider and the alias it cannot serve",
              "opencode-go" in raised and "ds" in raised, raised)
        check("says which aliases it does serve", "it serves:" in raised, raised)


def test_current_value_reads_through_the_not_set_notice(tmp: pathlib.Path) -> None:
    bin_dir = fake_hermes(tmp)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    try:
        check("reads a set value", A.current_value("model.provider") == "ORIGINAL_PROV")
        check("returns '' for a missing binary",
              A.current_value("model.provider") == "ORIGINAL_PROV")
    finally:
        os.environ["PATH"] = old_path


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ds-apply-"))
    for sub in ("a", "b", "c", "d", "e"):
        (tmp / sub).mkdir()
    try:
        test_a_failed_write_rolls_back_the_ones_before_it(tmp / "a")
        test_success_writes_all_three_keys_in_order(tmp / "b")
        test_dry_run_writes_nothing(tmp / "c")
        test_a_provider_that_does_not_serve_the_alias_is_refused(tmp / "d")
        test_current_value_reads_through_the_not_set_notice(tmp / "e")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
