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


def fake_hermes(tmp: pathlib.Path, *, fail_on: str = "", calls: pathlib.Path | None = None,
                fail_get: str = "", not_set: str = "") -> str:
    """A stand-in `hermes` that can be made to fail on a chosen key.

    `fail_on` makes a `config set` of that key fail, `fail_get` makes a `config get`
    of that key fail (the read-failure path), and `not_set` makes a `config get`
    print Hermes' "not set" notice, which is a real answer rather than a failure.
    """
    # Quote the redirect target: TMPDIR is allowed to contain spaces, and an
    # unquoted path silently sends the log somewhere else.
    log = f'echo "$*" >> "{calls}"\n' if calls else ""
    # Only emit the failing branch when a failure is actually wanted: an empty
    # pattern would match every "config set" call.
    fail_branch = f'  *"config set {fail_on}"*)      echo "boom" >&2; exit 1 ;;\n' if fail_on else ""
    fail_get_branch = f'  *"config get {fail_get}"*)      echo "keyring locked" >&2; exit 1 ;;\n' if fail_get else ""
    not_set_branch = f'  *"config get {not_set}"*)        echo "config key not set"; exit 0 ;;\n' if not_set else ""
    script = f"""#!/bin/sh
{log}case "$*" in
{fail_get_branch}{not_set_branch}  *"config get model.provider"*) echo "ORIGINAL_PROV"; exit 0 ;;
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
          "config set model.provider ORIGINAL_PROV" in written,
          f"log={written!r} path={calls}")
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
    finally:
        os.environ["PATH"] = old_path
    # A "not set" notice is an answer, not a failure: the key is genuinely unset.
    (tmp / "notice").mkdir(exist_ok=True)
    notice_dir = fake_hermes(tmp / "notice", not_set="model.base_url")
    os.environ["PATH"] = f"{notice_dir}:{old_path}"
    try:
        check("a 'not set' notice reads as unset, not as an error",
              A.current_value("model.base_url") == "")
    finally:
        os.environ["PATH"] = old_path
    # No hermes binary at all is a read FAILURE, not "unset". It used to return
    # the same '' as an unset key, which is the bug this pins.
    bare = tmp / "bare"
    bare.mkdir()
    os.environ["PATH"] = str(bare)
    try:
        try:
            A.current_value("model.provider")
            raised = None
        except A.ConfigReadError as exc:
            raised = str(exc)
    finally:
        os.environ["PATH"] = old_path
    check("a missing hermes binary raises rather than reading as unset",
          raised is not None and "model.provider" in (raised or ""), str(raised))
    check("the message names the binary", "hermes" in (raised or ""), str(raised))


def test_a_failed_config_read_raises_instead_of_looking_unset(tmp: pathlib.Path) -> None:
    """A read that fails must not be the same value as a key that is unset. Both
    cases used to be '', and the router then treated a healthy sticky provider as
    absent, which frees it to move the conversation."""
    bin_dir = fake_hermes(tmp, fail_get="model.provider")
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    try:
        try:
            A.current_provider()
            raised = None
        except A.ConfigReadError as exc:
            raised = str(exc)
    finally:
        os.environ["PATH"] = old_path
    check("an unreadable provider raises", raised is not None)
    check("the message names the key and the reason",
          "model.provider" in (raised or "") and "keyring locked" in (raised or ""), str(raised))


def test_an_unreadable_previous_value_stops_the_write_entirely(tmp: pathlib.Path) -> None:
    """The rollback restores what it read. If a value cannot be read, a later
    failure would restore it as 'unset', deleting a setting while reporting it as
    restored. Refusing to start is the only safe answer."""
    calls = tmp / "calls.log"
    bin_dir = fake_hermes(tmp, fail_get="model.base_url", calls=calls)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    try:
        try:
            A.set_provider("clinepass", {"base_url": "https://x/v1"}, "ds",
                           dry=False, models={"ds": {"clinepass": "cline-pass/ds"}})
            raised = None
        except A.ConfigReadError as exc:
            raised = str(exc)
    finally:
        os.environ["PATH"] = old_path
    check("refuses when a value it would have to restore cannot be read", raised is not None)
    check("the refusal explains why", "Refusing to write" in (raised or ""), str(raised))
    written = calls.read_text() if calls.exists() else ""
    check("nothing was written at all", "config set" not in written, repr(written))
    check("and nothing was unset", "config unset" not in written, repr(written))


def test_main_leaves_the_config_alone_when_the_sticky_read_fails(tmp: pathlib.Path) -> None:
    """The sticky provider is the whole reason a healthy conversation is left where
    it is. An unreadable one must stop the run rather than move it."""
    calls = tmp / "calls.log"
    bin_dir = fake_hermes(tmp, fail_get="model.provider", calls=calls)
    cfg_dir = tmp / "cfg"
    cfg_dir.mkdir()
    real_config = A.CONFIG
    A.CONFIG = cfg_dir / "config.yaml"
    A.CONFIG.write_text("default_model: ds\ndefault_provider: clinepass\n"
                        "providers:\n  clinepass:\n    base_url: https://x/v1\n    key_env: K\n"
                        "models:\n  ds:\n    clinepass: cline-pass/ds\n")
    old_path, old_argv = os.environ["PATH"], sys.argv
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    sys.argv = ["apply.py"]
    try:
        code = A.main()
    finally:
        os.environ["PATH"] = old_path
        sys.argv = old_argv
        A.CONFIG = real_config
    check("exits with the read-failure code", code == 6, str(code))
    written = calls.read_text() if calls.exists() else ""
    check("wrote nothing", "config set" not in written, repr(written))
    check("did not consult the router either",
          not any("router.py" in line for line in written.splitlines()), repr(written))


def test_a_malformed_config_reports_instead_of_crashing(tmp: pathlib.Path) -> None:
    """A stranger's first edit to config.yaml is a typo. A traceback there is worse
    than a message, because the timer swallows the traceback into the journal."""
    real_config = A.CONFIG
    try:
        for text, fragment in (("{not yaml: [", "not valid YAML"),
                               ("providers: 3\n", "providers"),
                               ("providers:\n  clinepass: 3\n", "providers.clinepass"),
                               ("models:\n  ds: 3\n", "models.ds")):
            A.CONFIG = tmp / f"cfg-{abs(hash(text))}.yaml"
            A.CONFIG.write_text(text)
            try:
                A.load_config()
                raised = None
            except A.ConfigError as exc:
                raised = str(exc)
            check(f"a malformed config is diagnosed ({fragment})",
                  raised is not None and fragment in raised, f"{text!r} -> {raised}")
        A.CONFIG = tmp / "does-not-exist.yaml"
        try:
            A.load_config()
            raised = None
        except A.ConfigError as exc:
            raised = str(exc)
        check("a missing config is diagnosed, not crashed",
              raised is not None and "cannot read" in raised, str(raised))
    finally:
        A.CONFIG = real_config


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ds-apply-"))
    for sub in ("a", "b", "c", "d", "e", "f", "g", "h", "i"):
        (tmp / sub).mkdir()
    try:
        test_a_failed_write_rolls_back_the_ones_before_it(tmp / "a")
        test_success_writes_all_three_keys_in_order(tmp / "b")
        test_dry_run_writes_nothing(tmp / "c")
        test_a_provider_that_does_not_serve_the_alias_is_refused(tmp / "d")
        test_current_value_reads_through_the_not_set_notice(tmp / "e")
        test_a_failed_config_read_raises_instead_of_looking_unset(tmp / "f")
        test_an_unreadable_previous_value_stops_the_write_entirely(tmp / "g")
        test_main_leaves_the_config_alone_when_the_sticky_read_fails(tmp / "h")
        test_a_malformed_config_reports_instead_of_crashing(tmp / "i")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
