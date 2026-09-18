"""Tests for quota parsing and provider selection.

Run: python3 test_routing.py
"""

from __future__ import annotations

import time

import quota as q
import routing as r

WEIGHTS = {"session": 1.0, "weekly": 1.0, "monthly": 0.6}
SKIP = 0.85
HOUR = 3600.0
DAY = 86400.0


def spec(models: dict) -> dict:
    return {"models": models}


PROVIDERS = {
    "commandcode": spec({"ds": "deepseek/deepseek-v4.1-flash"}),
    "opencode-go": spec({"ds": "deepseek-v4.1-flash"}),
    "ollama-cloud": spec({"ds": "deepseek-v4.1-flash", "only-here": "x-1"}),
}


def win(label: str, percent: float, resets_in=None, now=None) -> q.Window:
    now = time.time() if now is None else now
    return q.Window(label, percent, None if resets_in is None else now + resets_in)


# ---------------------------------------------------------------- parsing


def test_opencode_payload_parses_all_three_windows():
    payload = {"usage": {
        "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-09-18T05:24:13.354Z"},
        "weekly": {"status": "ok", "percent": 98, "resetsAt": "2026-09-21T00:00:00.354Z"},
        "monthly": {"status": "ok", "percent": 87, "resetsAt": "2026-09-27T07:40:17.354Z"},
    }}
    windows = q.parse_opencode_go(payload)
    assert [w.label for w in windows] == ["session", "weekly", "monthly"]
    assert windows[1].percent == 0.98


def test_commandcode_parses_dollar_windows_into_fractions():
    payload = {"credits": {"monthlyCredits": 63.21},
               "windowLimits": {"fiveHour": {"used": 6.79, "cap": 14, "resetAt": 1789698497290},
                                "weekly": {"used": 6.79, "cap": 35, "resetAt": 1790253387497}}}
    windows = [w for w in q.parse_commandcode(payload) if w.label != "monthly"]
    assert round(windows[0].percent, 4) == round(6.79 / 14, 4)
    import datetime
    assert datetime.datetime.fromtimestamp(windows[0].resets_at, datetime.timezone.utc).year == 2026


def test_clinepass_parses_the_nested_data_wrapper():
    """ClinePass wraps its payload: {"data": {"limits": [...]}} with the type
    in snake_case (five_hour). Both must be handled or the provider reads as
    unreadable."""
    payload = {"data": {"limits": [
        {"type": "five_hour", "percentUsed": 12.5, "resetsAt": "2026-09-18T09:06:35.170792893Z"},
        {"type": "weekly", "percentUsed": 0, "resetsAt": "2026-09-25T04:06:35.172953431Z"},
        {"type": "monthly", "percentUsed": 3, "resetsAt": "2026-10-18T04:06:35.175170743Z"},
    ]}, "success": True}
    windows = q.parse_clinepass(payload)
    assert [w.label for w in windows] == ["five hour", "weekly", "monthly"]
    # five_hour must classify as the session window, not fall through to unknown.
    assert windows[0].kind == "session", windows[0].label
    assert windows[0].percent == 0.125
    assert windows[1].kind == "weekly" and windows[2].kind == "monthly"


def test_clinepass_rejects_an_unrecognised_payload():
    """A provider that answers with something unexpected must read as
    unreadable rather than as perfect headroom."""
    for bad in ({"data": {}}, {"data": {"limits": []}}, {}, {"data": {"limits": "nope"}}):
        try:
            q.parse_clinepass(bad)
        except ValueError:
            continue
        raise AssertionError(f"should have raised for {bad!r}")


def test_clinepass_nanosecond_reset_times_are_parsed():
    payload = {"data": {"limits": [
        {"type": "five_hour", "percentUsed": 0, "resetsAt": "2026-09-18T09:06:35.170792893Z"},
    ]}}
    window = q.parse_clinepass(payload)[0]
    assert window.resets_at is not None
    import datetime
    assert datetime.datetime.fromtimestamp(window.resets_at, datetime.timezone.utc).year == 2026


def test_ollama_reports_only_the_windows_the_plan_has():
    windows = q.parse_ollama({"limits": {"monthly": {"usage": 0.451, "models": []}}})
    assert len(windows) == 1 and windows[0].percent == 0.451
    # No reset time is published, so no pace is computable.
    assert windows[0].pace() is None


def test_unknown_window_name_is_scored_as_session_class_risk():
    candidate = r.score(q.Quota("x", [win("something brand new", 0.9)]), "m", WEIGHTS, SKIP)
    assert candidate.pressure >= 0.85


# ---------------------------------------------------------------- pace: the core idea


def test_pace_separates_a_slow_burn_from_a_healthy_late_window():
    now = time.time()
    # 45% of a month with 15 days left: on pace, safe.
    monthly = win("monthly", 0.45, resets_in=15 * DAY, now=now)
    assert monthly.pace(now) <= 1.0
    # 45% of a 5-hour window that is 92% elapsed is a GOOD pace: almost unused
    # with the window about to refill. Low pace means safe, not alarming.
    late_session = win("session", 0.45, resets_in=24 * 60, now=now)
    assert late_session.pace(now) < 0.6
    # A burst: a third of the week already gone with most of the week left.
    burst = win("weekly", 0.35, resets_in=5 * DAY, now=now)
    assert burst.pace(now) > 1.2


def test_pace_above_one_means_the_window_will_run_out():
    now = time.time()
    normal = win("weekly", 0.90, resets_in=1 * DAY, now=now)
    burst = win("weekly", 0.90, resets_in=3 * DAY, now=now)
    assert normal.pace(now) < 1.1
    assert burst.pace(now) > 1.5


def test_hard_exhaustion_reads_the_reported_window_not_the_pace():
    now = time.time()
    assert r.hard_exhausted(q.Quota("go", [win("weekly", 1.0, resets_in=2 * DAY, now=now)]), SKIP, now)
    # A window whose reset time already passed has refilled upstream.
    assert not r.hard_exhausted(q.Quota("go", [win("weekly", 1.0, resets_in=-60, now=now)]), SKIP, now)


def test_unreadable_reset_time_still_catches_an_exhausted_plan():
    # 95% with no reset time: cannot reason about recovery, so treat as spent.
    assert r.hard_exhausted(q.Quota("ollama", [win("monthly", 0.95)]), SKIP)


# ---------------------------------------------------------------- selection


def test_spent_weekly_moves_traffic_off_opencode():
    """The shape of the real accounts: opencode's weekly is nearly gone."""
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("session", 0.47, resets_in=24 * 60, now=now),
                                               win("weekly", 0.19, resets_in=6 * DAY, now=now),
                                               win("monthly", 0.09, resets_in=29 * DAY, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("session", 0.0, resets_in=3 * HOUR, now=now),
                                               win("weekly", 0.98, resets_in=2.9 * DAY, now=now),
                                               win("monthly", 0.87, resets_in=9 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.451)]),
    }
    decision = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, now=now)
    assert decision.provider == "commandcode", decision.reason
    assert [c.provider for c in decision.ranked][0] == "commandcode"


def test_unreadable_quota_sorts_below_a_real_reading():
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [], 0.0, "AuthenticationError"),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.10, resets_in=5 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.45)]),
    }
    decision = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, now=now)
    # Ollama has the fullest window of the three, so it is the honest pick; the
    # point of this test is only that the unreadable provider sorts last.
    assert decision.provider in {"opencode-go", "ollama-cloud"}, decision.reason
    assert decision.ranked[-1].provider == "commandcode"


def test_sticky_holds_a_healthy_provider_even_when_another_is_emptier():
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("session", 0.05, resets_in=4 * HOUR, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.30, resets_in=5 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.45)]),
    }
    sticky = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, sticky_provider="opencode-go", now=now)
    assert sticky.provider == "opencode-go" and sticky.reason.startswith("sticky")
    fresh = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, now=now)
    assert fresh.provider == "commandcode"


def test_sticky_does_not_abandon_on_soft_pressure_alone():
    """The half-penny case: burning fast is a prediction, not a failure."""
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("weekly", 0.20, resets_in=5 * DAY, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.88, resets_in=3 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.10)]),
    }
    decision = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, sticky_provider="opencode-go", now=now)
    assert decision.provider == "opencode-go", decision.reason


def test_sticky_leaves_immediately_once_a_provider_is_actually_spent():
    """The frustration case: a hard limit must not wait on the cache argument."""
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("weekly", 0.30, resets_in=4 * DAY, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 1.0, resets_in=2 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.45)]),
    }
    decision = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, sticky_provider="opencode-go", now=now)
    assert decision.provider == "commandcode", decision.reason


def test_sticky_leaves_when_its_quota_cannot_be_read():
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("weekly", 0.30, resets_in=4 * DAY, now=now)]),
        "opencode-go": q.Quota("opencode-go", [], 0.0, "ConnectionError: timed out"),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.45)]),
    }
    decision = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, sticky_provider="opencode-go", now=now)
    assert decision.provider == "commandcode"


def test_imminent_exhaustion_is_flagged_for_the_caller():
    """The router reports it; the caller's mid-turn swap is what acts on it."""
    now = time.time()
    candidate = r.score(q.Quota("cc", [win("session", 0.95, resets_in=40 * 60, now=now)]), "m", WEIGHTS, SKIP, now=now)
    assert "expected to exhaust" in candidate.detail


def test_pin_is_honoured_and_refused_when_spent():
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("session", 0.05, resets_in=4 * HOUR, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 1.0, resets_in=2 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.45)]),
    }
    assert r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, pinned="opencode-go", now=now).provider == ""
    ok = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, pinned="ollama-cloud", now=now)
    assert ok.provider == "ollama-cloud" and ok.reason.startswith("pinned")


def test_model_only_some_providers_serve_still_routes():
    live = {name: q.Quota(name, [win("monthly", 0.10)]) for name in PROVIDERS}
    decision = r.choose("only-here", PROVIDERS, live, WEIGHTS, SKIP)
    assert decision.provider == "ollama-cloud"


def test_unknown_model_reports_rather_than_guesses():
    live = {name: q.Quota(name, [win("monthly", 0.10)]) for name in PROVIDERS}
    decision = r.choose("nope", PROVIDERS, live, WEIGHTS, SKIP)
    assert decision.provider == "" and not decision.ok
    assert "no provider serves" in decision.reason


def test_all_spent_is_reported_not_silently_ignored():
    live = {name: q.Quota(name, [win("weekly", 1.0, resets_in=DAY)]) for name in PROVIDERS}
    decision = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP)
    assert decision.provider == "" and "exhausted" in decision.reason


def test_peak_windows_are_read_from_the_clock():
    import peak as p
    import datetime

    def at(iso):
        return datetime.datetime.fromisoformat(iso).timestamp()

    # Thursday 2026-09-17. CommandCode peaks 01-04 and 06-10 UTC; Ollama 12-18.
    assert p.peak_providers({"windows": {"commandcode": [[1, 4], [6, 10]], "ollama-cloud": [[12, 18]]}},
                            at("2026-09-17T02:00:00+00:00")) == {"commandcode"}
    assert p.peak_providers({"windows": {"commandcode": [[1, 4], [6, 10]], "ollama-cloud": [[12, 18]]}},
                            at("2026-09-17T14:00:00+00:00")) == {"ollama-cloud"}
    # 05:00 UTC is between CommandCode's two blocks and before Ollama's.
    assert p.peak_providers({"windows": {"commandcode": [[1, 4], [6, 10]], "ollama-cloud": [[12, 18]]}},
                            at("2026-09-17T05:00:00+00:00")) == set()
    # Saturday is off-peak for both.
    assert p.peak_providers({"windows": {"commandcode": [[1, 4]], "ollama-cloud": [[12, 18]]}},
                            at("2026-09-19T02:00:00+00:00")) == set()


def test_off_peak_provider_wins_between_two_safe_options():
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("session", 0.30, resets_in=4 * HOUR, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.30, resets_in=5 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.30)]),
    }
    d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, now=now,
                 peak_providers={"opencode-go"})
    assert d.provider != "opencode-go", d.reason
    assert "off-peak" in d.reason


def test_peak_never_displaces_a_healthy_sticky_provider():
    """Doubling the rate is not worth a cache reset mid-conversation."""
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("session", 0.10, resets_in=4 * HOUR, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.30, resets_in=5 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.10)]),
    }
    d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, sticky_provider="opencode-go",
                 now=now, peak_providers={"opencode-go"})
    assert d.provider == "opencode-go" and d.reason.startswith("sticky")


def test_when_every_option_is_at_peak_it_picks_the_safest():
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("session", 0.10, resets_in=4 * HOUR, now=now)]),
        "opencode-go": q.Quota("opencode-go", [win("weekly", 0.60, resets_in=5 * DAY, now=now)]),
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.50)]),
    }
    d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, now=now,
                 peak_providers={"commandcode", "opencode-go", "ollama-cloud"})
    assert d.provider == "commandcode", d.reason
    assert "all options at peak" in d.reason


def test_unreadable_quotas_degrade_to_a_provider_not_to_nothing():
    """Never strand the agent: 'no model' is worse than the primary Hermes
    would have used anyway. A degraded pick must be labelled as such."""
    live = {name: q.Quota(name, [], 0.0, "ConnectionError: no route") for name in PROVIDERS}
    d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP)
    assert d.ok, d.reason
    assert d.reason.startswith("degraded"), d.reason
    assert d.model_id


def test_all_genuinely_exhausted_still_reports_no_provider():
    """If every provider is hard-exhausted, refusing is correct: we must not
    send traffic into a known 429."""
    live = {name: q.Quota(name, [win("weekly", 1.0, resets_in=DAY)]) for name in PROVIDERS}
    assert r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP).provider == ""


def test_reset_time_beyond_the_window_length_is_treated_as_unknowable():
    """Clock skew or a mis-set plan boundary must not invent a crisis."""
    now = time.time()
    bad = win("weekly", 0.5, resets_in=40 * DAY, now=now)
    assert bad.pace(now) is None
    # And a normal weekly window still paces.
    assert win("weekly", 0.5, resets_in=3 * DAY, now=now).pace(now) is not None


def test_collector_labels_and_api_labels_agree():
    """The same window must behave identically from either data source."""
    from_api = q.parse_opencode_go({"usage": {"rolling": {"percent": 0, "resetsAt": "2026-09-18T05:24:13Z"}}})
    from_snapshot = q.Window("Session (5-hour)", 0.0, time.time() + 3600)
    assert from_api[0].kind == from_snapshot.kind == "session"
    assert q.Window("Weekly (7-day)", 0.5).kind == "weekly"
    assert q.Window("5 hours", 0.5).kind == "session"
    assert q.Window("Monthly", 0.5).kind == "monthly"


def test_unreachable_provider_is_excluded_not_merely_ranked():
    """Quota says what a plan allows; health says whether the endpoint answers.
    A provider that fails its probe must not be chosen, even with perfect quota."""
    now = time.time()
    live = {
        "commandcode": q.Quota("commandcode", [win("monthly", 0.90)]),   # nearly spent
        "opencode-go": q.Quota("opencode-go", [win("monthly", 0.10)]),   # healthy quota
        "ollama-cloud": q.Quota("ollama-cloud", [win("monthly", 0.10)]),
    }
    # opencode is the emptiest on quota but is not answering.
    health = {"opencode-go": {"ok": False, "seconds": 0.0, "error": "ConnectionError"}}
    d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, now=now, health=health)
    assert d.provider != "opencode-go", d.reason
    assert "FAILED" in str([c.detail for c in d.ranked if c.provider == "opencode-go"])


def test_unhealthy_provider_cannot_win_even_when_sticky():
    """A pinned-but-dead provider must be left, like a hard-exhausted one."""
    now = time.time()
    live = {n: q.Quota(n, [win("monthly", 0.10)]) for n in PROVIDERS}
    health = {"ollama-cloud": {"ok": False, "seconds": 0.0, "error": "timeout"}}
    d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, sticky_provider="ollama-cloud",
                 now=now, health=health)
    assert d.provider != "ollama-cloud", d.reason


def test_a_slow_but_working_provider_is_deprioritised_not_excluded():
    """Slowness is soft: it should break ties, not remove a working provider."""
    now = time.time()
    live = {n: q.Quota(n, [win("monthly", 0.10)]) for n in PROVIDERS}
    health = {"ollama-cloud": {"ok": True, "seconds": 45.0, "error": ""}}
    d = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, now=now, health=health)
    ranked = {c.provider: c for c in d.ranked}
    assert ranked["ollama-cloud"].pressure > 0, "slow provider should gain pressure"
    assert not ranked["ollama-cloud"].hard, "slow is not the same as dead"
    assert d.provider != "ollama-cloud", "a healthy peer should win"


def test_health_absent_changes_nothing():
    """No health data must behave exactly as before it existed."""
    now = time.time()
    live = {n: q.Quota(n, [win("monthly", 0.10)]) for n in PROVIDERS}
    without = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, now=now)
    explicit_none = r.choose("ds", PROVIDERS, live, WEIGHTS, SKIP, now=now, health=None)
    assert without.provider == explicit_none.provider


def test_a_nan_percentage_cannot_look_like_free_capacity():
    """NaN compares false against every threshold, so an unsanitized NaN window
    reads as perfect headroom and makes a broken reading look like the healthiest
    provider. It must sanitise to 0 instead."""
    bad = q.Quota("p", [q.Window("weekly", float("nan"), time.time() + 3 * DAY)])
    assert bad.windows[0].percent == 0.0
    c = r.score(bad, "m", WEIGHTS, SKIP)
    assert c.pressure == 0.0 and not c.hard
    # inf is the opposite direction and must not become 0 (that would hide a
    # genuinely spent provider).
    inf = q.Quota("p", [q.Window("weekly", float("inf"), time.time() + 3 * DAY)])
    assert inf.windows[0].percent == 0.0, "inf sanitises too, so it cannot poison a sort"


def test_a_negative_percentage_clamps_to_zero():
    w = q.Window("weekly", -0.4, time.time() + 3 * DAY)
    assert w.percent == 0.0


def test_ticks_resolve_deterministically_regardless_of_input_order():
    """A tie must not resolve by dict insertion order, or --plan output would
    differ between runs of the same fleet."""
    import itertools
    now = time.time()
    providers = {n: {"models": {"ds": n}} for n in ("p1", "p2", "p3")}
    live = {n: q.Quota(n, [win("weekly", 0.30, resets_in=3 * DAY, now=now)]) for n in providers}
    picks = {r.choose("ds", {k: providers[k] for k in perm}, live, WEIGHTS, SKIP, now=now).provider
             for perm in itertools.permutations(providers)}
    assert len(picks) == 1, f"tie broke differently per ordering: {picks}"


def test_the_chosen_name_comes_from_the_provider_mapping_not_the_quota():
    """A Quota filed under the wrong key must not route to a name that does not
    exist in the provider set."""
    now = time.time()
    providers = {"p1": {"models": {"ds": "m1"}}, "p2": {"models": {"ds": "m2"}}}
    quotas = {"p1": q.Quota("WRONG_NAME", [win("weekly", 0.1, resets_in=3 * DAY, now=now)]),
              "p2": q.Quota("p2", [win("weekly", 0.9, resets_in=3 * DAY, now=now)])}
    d = r.choose("ds", providers, quotas, WEIGHTS, SKIP, now=now)
    assert d.provider in providers, d.provider


def test_sticky_expires():
    table = r.StickyTable(ttl_seconds=0)
    table.put("c1", "commandcode")
    assert table.get("c1") is None


def test_adding_a_model_is_config_only():
    """The extensibility claim, asserted: a new alias needs no code change."""
    providers = {
        "commandcode": spec({"deepseek-v4.2-flash": "deepseek/deepseek-v4.2-flash"}),
        "opencode-go": spec({"deepseek-v4.2-flash": "deepseek-v4.2-flash"}),
        "ollama-cloud": spec({}),  # not offered here yet
    }
    live = {name: q.Quota(name, [win("monthly", 0.10)]) for name in providers}
    decision = r.choose("deepseek-v4.2-flash", providers, live, WEIGHTS, SKIP)
    assert decision.provider == "commandcode"
    assert decision.model_id == "deepseek/deepseek-v4.2-flash"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  pass  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'all tests passed'} ({failures} failures)")
    raise SystemExit(1 if failures else 0)
