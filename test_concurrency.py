"""Concurrency pressure tests.

A provider's concurrency cap applies to requests in flight, not to sessions
that merely exist. Ollama Cloud Pro allows 3 and queues beyond that, rejecting
when the queue is full, so being over the cap is real but soft pressure.
"""

from __future__ import annotations

import time

import quota as q
import routing as r

WEIGHTS = {"session": 1.0, "weekly": 1.0, "monthly": 0.6}
SKIP = 0.85
DAY = 86400.0
HOUR = 3600.0
CAPS = {"ollama-cloud": 3}
PROVIDERS = {n: {"models": {"ds": n}} for n in ("commandcode", "opencode-go", "ollama-cloud")}


def win(label, percent, resets_in=None, now=None):
    now = time.time() if now is None else now
    return q.Window(label, percent, None if resets_in is None else now + resets_in)


def healthy_quotas(now):
    """All three healthy, equal windows, so only load can separate them."""
    return {
        "commandcode": q.Quota("commandcode", [win("monthly", 0.20)]),
        "opencode-go": q.Quota("opencode-go", [win("monthly", 0.20)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.20)]),
    }


def test_under_cap_adds_no_pressure():
    """At or below the cap, load contributes nothing at all."""
    now = time.time()
    # An on-pace window contributes zero risk, so any pressure must come from load.
    baseline = r.score(q.Quota("ollama-cloud", [win("weekly", 0.20, resets_in=5 * DAY, now=now)]),
                       "m", WEIGHTS, SKIP, now=now)
    at_cap = r.score(q.Quota("ollama-cloud", [win("weekly", 0.20, resets_in=5 * DAY, now=now)]),
                     "m", WEIGHTS, SKIP, now=now, active_sessions=3, concurrency_cap=3)
    assert baseline.pressure == at_cap.pressure == 0.0


def test_over_cap_adds_pressure_and_says_so():
    now = time.time()
    c = r.score(q.Quota("ollama-cloud", [win("monthly", 0.20)]), "m", WEIGHTS, SKIP,
                now=now, active_sessions=6, concurrency_cap=3)
    assert c.pressure > 0, c.detail
    assert "6 active / cap 3" in c.detail and "+3 queued" in c.detail


def test_load_pressure_is_capped_and_never_excludes_a_provider():
    """A badly overloaded provider should lose, not vanish: queueing still works,
    so it must remain usable when everything else is worse."""
    now = time.time()
    c = r.score(q.Quota("ollama-cloud", [win("monthly", 0.20)]), "m", WEIGHTS, SKIP,
                now=now, active_sessions=50, concurrency_cap=3)
    assert c.pressure <= 0.6
    assert not c.hard, "over-capacity is soft pressure, not hard exhaustion"


def test_load_breaks_a_tie_between_equal_quota_providers():
    """The whole point: with identical windows, the fuller provider loses."""
    now = time.time()
    d = r.choose("ds", PROVIDERS, healthy_quotas(now), WEIGHTS, SKIP,
                 now=now, load={"ollama-cloud": 5}, concurrency_caps=CAPS)
    assert d.provider != "ollama-cloud", d.reason


def test_load_never_beats_a_window_that_will_actually_run_out():
    """Queueing is a latency cost; a window predicted to exhaust within hours is
    a hard stop. So a modest queue overage must not outrank it."""
    now = time.time()
    quotas = {
        # ~13 hours to exhaustion at the current burn.
        "commandcode": q.Quota("commandcode", [win("weekly", 0.90, resets_in=2 * DAY, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.90, resets_in=2 * DAY, now=now)]),
        # Genuinely fine on quota: a fraction of the month, no reset published.
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.05)]),
    }
    d = r.choose("ds", PROVIDERS, quotas, WEIGHTS, SKIP,
                 now=now, load={"ollama-cloud": 5}, concurrency_caps=CAPS)
    assert d.provider == "ollama-cloud", d.reason


def test_sticky_stays_on_an_over_cap_provider():
    """Being one over a queue cap is not worth resetting a conversation's
    prompt cache for; hard exhaustion is."""
    now = time.time()
    d = r.choose("ds", PROVIDERS, healthy_quotas(now), WEIGHTS, SKIP,
                 sticky_provider="ollama-cloud", now=now,
                 load={"ollama-cloud": 4}, concurrency_caps=CAPS)
    assert d.provider == "ollama-cloud", d.reason


def test_no_caps_declared_means_no_load_scoring():
    now = time.time()
    d = r.choose("ds", PROVIDERS, healthy_quotas(now), WEIGHTS, SKIP,
                 now=now, load={"ollama-cloud": 50}, concurrency_caps={})
    assert d.ok  # never refuses merely because load was supplied


if __name__ == "__main__":
    failures = 0
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  pass  {name}")
                passed += 1
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'all tests passed'} "
          f"({passed} passed, {failures} failed)")
    raise SystemExit(1 if failures else 0)
