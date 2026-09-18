"""The stickiness contract the CLI depends on.

ds-switch reads the current provider from Hermes config and passes it as
--sticky. These assert the two properties that make that safe: a healthy
provider is never left, and an exhausted one always is.
"""

from __future__ import annotations

import time

import quota as q
import routing as r

WEIGHTS = {"session": 1.0, "weekly": 1.0, "monthly": 0.6}
SKIP = 0.85
DAY = 86400.0
HOUR = 3600.0

PROVIDERS = {n: {"models": {"ds": n}} for n in ("commandcode", "opencode-go", "ollama-cloud")}


def win(label, percent, resets_in=None, now=None):
    now = time.time() if now is None else now
    return q.Window(label, percent, None if resets_in is None else now + resets_in)


def test_sticky_survives_repeated_runs_with_no_real_reason_to_move():
    """A cron calling this every 15 minutes must not churn the provider."""
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("session", 0.20, resets_in=4 * HOUR, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.25, resets_in=5 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.30)]),
    }
    for _ in range(5):
        d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, sticky_provider="commandcode", now=now)
        assert d.provider == "commandcode", d.reason


def test_sticky_moves_once_the_window_is_actually_spent():
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("session", 1.0, resets_in=3 * HOUR, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.25, resets_in=5 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.30)]),
    }
    d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, sticky_provider="commandcode", now=now)
    assert d.provider != "commandcode", d.reason


def test_peak_alone_never_causes_a_mid_conversation_move():
    """Off-peak is a tie-break for a FRESH pick, not a reason to abandon."""
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("session", 0.50, resets_in=3 * HOUR, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.10, resets_in=6 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.10)]),
    }
    d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, sticky_provider="commandcode",
                 now=now, peak_providers={"commandcode"})
    assert d.provider == "commandcode", d.reason


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
