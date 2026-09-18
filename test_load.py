#!/usr/bin/env python3
"""Tests for load.py: the concurrency reading taken from Hermes' session store.

This module had no tests at all, and it feeds the only term that prices a
provider as saturated. The two failures it used to have were invisible: sessions
of a named custom provider were filed under a provider identity no cap is ever
declared for, and a store that could not be read produced the same empty answer
as a store with nothing running.

Run: python3 test_load.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import load as L

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  pass  {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


def make_store(path: Path, *, leases=(), sessions=(), with_leases_table=True,
               with_sessions_table=True) -> Path:
    """A minimal stand-in for Hermes' state.db.

    `leases` are (conversation_id, acquired_at, expires_at) and `sessions` are
    (id, session_key, billing_provider, model_config, last_activity_at).
    """
    con = sqlite3.connect(path)
    if with_sessions_table:
        con.execute("create table sessions (id text primary key, session_key text, "
                    "billing_provider text, model_config text, last_activity_at real)")
    if with_leases_table:
        con.execute("create table session_turn_leases (conversation_id text, holder text, "
                    "acquired_at real, expires_at real)")
    con.executemany("insert into sessions values (?,?,?,?,?)", sessions)
    if leases:
        con.executemany("insert into session_turn_leases values (?,?,?,?)",
                        [(c, "h", a, e) for c, a, e in leases])
    con.commit()
    con.close()
    return path


def tmpdir(name: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"ds-load-{name}-"))


def test_a_named_custom_provider_is_counted_under_its_real_name() -> None:
    """Hermes writes the generic identity `custom` into billing_provider for every
    named custom provider, so grouping on that column filed those sessions under a
    provider that no cap is ever declared for. A cap on clinepass then never saw
    them, which is the opposite of what a cap is for."""
    now = time.time()
    store = make_store(tmpdir("custom") / "state.db",
                       leases=[("k1", now, now + 600)],
                       sessions=[("s1", "k1", "custom", '{"provider": "clinepass"}', now)])
    got = L.active_by_provider(session_db=store, now=now)
    check("counts the session under the durable provider name",
          got.count("clinepass") == 1, f"counts={got.counts} source={got.source}")
    check("does not invent a provider called 'custom'",
          "custom" not in got.counts, str(got.counts))
    check("reports the reading as readable", got.readable, got.error)


def test_a_builtin_provider_still_counts_from_billing_provider() -> None:
    """Built-in providers carry no model config, so the fallback has to hold."""
    now = time.time()
    store = make_store(tmpdir("builtin") / "state.db",
                       leases=[("k1", now, now + 600)],
                       sessions=[("s1", "k1", "commandcode", "", now)])
    got = L.active_by_provider(session_db=store, now=now)
    check("counts the built-in provider", got.count("commandcode") == 1, str(got.counts))


def test_a_session_with_no_readable_provider_is_not_invented() -> None:
    """`custom` with no provider in the model config names nothing. Counting it as a
    provider called "custom" hides it; counting nothing and saying so is honest."""
    now = time.time()
    store = make_store(tmpdir("unknown") / "state.db",
                       leases=[("k1", now, now + 600)],
                       sessions=[("s1", "k1", "custom", "{not json", now)])
    got = L.active_by_provider(session_db=store, now=now)
    check("no provider key is invented", got.counts == {}, str(got.counts))
    check("the unreadable row is reported", "no readable provider" in got.error, got.error)


def test_an_unreadable_store_is_not_a_store_with_nothing_running() -> None:
    """Both used to answer counts {} and source 'none'. A saturated provider hence
    looked idle exactly when the read failed."""
    now = time.time()
    empty = make_store(tmpdir("empty") / "state.db")
    idle = L.active_by_provider(session_db=empty, now=now)
    check("a healthy empty store reads as idle, not as a failure",
          idle.readable and idle.counts == {}, f"{idle.source} {idle.error}")

    broken = tmpdir("broken") / "state.db"
    broken.write_text("this is not a database")
    failed = L.active_by_provider(session_db=broken, now=now)
    check("a corrupt store reads as unreadable", not failed.readable, str(failed.source))
    check("and carries the reason", bool(failed.error), failed.error)
    check("the two answers are distinguishable", idle.readable != failed.readable)


def test_a_store_without_a_leases_table_falls_back_to_activity() -> None:
    now = time.time()
    store = make_store(tmpdir("activity") / "state.db", with_leases_table=False,
                       sessions=[("s1", "k1", "commandcode", "", now - 10)])
    got = L.active_by_provider(session_db=store, now=now)
    check("counts recent activity", got.count("commandcode") == 1, str(got.counts))
    check("names the weaker source", got.source == "activity", got.source)
    check("still readable", got.readable, got.error)

    stale = make_store(tmpdir("stale") / "state.db", with_leases_table=False,
                       sessions=[("s1", "k1", "commandcode", "", now - 3600)])
    quiet = L.active_by_provider(session_db=stale, now=now)
    check("old activity is not load", quiet.counts == {}, str(quiet.counts))


def test_an_expired_lease_is_history_not_load() -> None:
    now = time.time()
    store = make_store(tmpdir("expired") / "state.db",
                       leases=[("k1", now - 7200, now - 3600)],
                       sessions=[("s1", "k1", "commandcode", "", now - 7200)])
    got = L.active_by_provider(session_db=store, now=now)
    check("a long-dead lease is not counted", got.counts == {}, str(got.counts))


def test_no_store_at_all_is_reported_as_such(tmp_path: Path | None = None) -> None:
    missing = tmpdir("missing") / "state.db"
    got = L.active_by_provider(session_db=missing, now=time.time())
    check("names the missing store", got.source == "no-store", got.source)
    check("is not readable", not got.readable, got.source)
    check("says which path was expected", "state.db" in got.error, got.error)


def test_the_store_is_only_ever_read() -> None:
    """load.py is documented as read-only. Prove it: the file's bytes must be
    identical after a read."""
    now = time.time()
    store = make_store(tmpdir("readonly") / "state.db",
                       leases=[("k1", now, now + 600)],
                       sessions=[("s1", "k1", "commandcode", "", now)])
    before = store.read_bytes()
    L.active_by_provider(session_db=store, now=now)
    check("the store is unchanged", store.read_bytes() == before)


def test_over_capacity_counts_the_overflow() -> None:
    load = L.Load({"ollama-cloud": 5, "commandcode": 1}, "leases")
    over = L.over_capacity(load, {"ollama-cloud": 3, "commandcode": 3, "clinepass": 1})
    check("a provider over its cap reports the overflow", over == {"ollama-cloud": 2}, str(over))
    check("a provider at its cap is not over it",
          L.over_capacity(L.Load({"ollama-cloud": 3}, "leases"), {"ollama-cloud": 3}) == {})
    check("an undeclared cap cannot be exceeded",
          L.over_capacity(L.Load({"ollama-cloud": 99}, "leases"), {}) == {})
    check("a nonsense cap is ignored here, and refused by the planner",
          L.over_capacity(L.Load({"ollama-cloud": 99}, "leases"), {"ollama-cloud": "three"}) == {})


def test_total_and_count_defaults() -> None:
    load = L.Load({"a": 2, "b": 1}, "leases")
    check("count of an absent provider is 0", load.count("nope") == 0)
    check("total adds up", load.total == 3, str(load.total))


def main() -> int:
    test_a_named_custom_provider_is_counted_under_its_real_name()
    test_a_builtin_provider_still_counts_from_billing_provider()
    test_a_session_with_no_readable_provider_is_not_invented()
    test_an_unreadable_store_is_not_a_store_with_nothing_running()
    test_a_store_without_a_leases_table_falls_back_to_activity()
    test_an_expired_lease_is_history_not_load()
    test_no_store_at_all_is_reported_as_such()
    test_the_store_is_only_ever_read()
    test_over_capacity_counts_the_overflow()
    test_total_and_count_defaults()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
