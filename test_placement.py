"""Placement tests: spreading sessions across providers under concurrency caps.

Every test asserts a behaviour, not a snapshot. The behaviours that matter:

* a cap is honoured by the *plan*, not merely priced;
* a session is NOT moved unless its provider is over cap, exhausted, unreadable,
  undeclared, or does not serve the alias (a move resets the prompt cache);
* a provider that does not serve the alias is never a destination;
* the same input always yields the same output;
* apply sends exactly the calls it should, skips what is already correct, and
  reports what it would do without writing when told to dry-run.

Run: python3 test_placement.py
"""

from __future__ import annotations

import sys

import json
import sqlite3
import tempfile
import time
from pathlib import Path

import placement as pl
import quota as q
import testkit

ALIAS = "ds"
OTHER_ALIAS = "only-here"

PROVIDERS = {
    "commandcode": {"models": {ALIAS: "deepseek/deepseek-v4.1-flash"}},
    "opencode-go": {"models": {ALIAS: "deepseek-v4.1-flash"}},
    "ollama-cloud": {"models": {ALIAS: "deepseek-v4.1-flash", OTHER_ALIAS: "x-1"}},
    "clinepass": {"models": {ALIAS: "cline-pass/deepseek-v4.1-flash"}},
}
CAPS = {"ollama-cloud": 3}


def tmp_path(*parts: str) -> Path:
    """A path under a fresh temp dir, for tests that must not read the real HOME."""
    base = Path(tempfile.mkdtemp(prefix="ds-placement-"))
    return base.joinpath(*parts)


def win(label: str, percent: float, resets_in=None, now=None) -> q.Window:
    now = time.time() if now is None else now
    return q.Window(label, percent, None if resets_in is None else now + resets_in)


def healthy(provider: str, percent: float = 0.10) -> q.Quota:
    """A readable, on-pace provider: no hard exhaustion, no pace risk."""
    return q.Quota(provider, [win("monthly", percent)])


def healthy_quotas(*extra: str) -> dict:
    names = set(PROVIDERS) | set(extra)
    return {name: healthy(name) for name in sorted(names)}


def sessions(*pairs) -> list[pl.Session]:
    """sessions(("oc1", "ollama-cloud"), ...)"""
    return [pl.Session(sid, provider) for sid, provider in pairs]


def on(session_list: list[pl.Session], provider: str) -> list[str]:
    return sorted(s.id for s in session_list if s.provider == provider)


def dest_counts(assignments) -> dict[str, int]:
    return pl.counts(assignments)


def moved(assignments) -> list[pl.Assignment]:
    return [a for a in assignments if a.changed]


# ------------------------------------------------------------------ planning


def test_over_cap_provider_redistributes_the_overflow_and_never_exceeds_the_cap():
    """Six sessions on a cap-3 provider: three keep their slot, three move."""
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(6)])
    result = pl.plan(fleet, healthy_quotas(), CAPS, PROVIDERS, ALIAS)

    assert dest_counts(result)["ollama-cloud"] == 3, dest_counts(result)
    assert len(moved(result)) == 3, [a.session_id for a in moved(result)]
    # And the overflow actually landed somewhere that can take it.
    spill = {a.provider for a in moved(result)}
    assert spill and "ollama-cloud" not in spill, spill
    # No provider anywhere in the plan is over a declared cap.
    for name, cap in CAPS.items():
        assert dest_counts(result).get(name, 0) <= cap


def test_under_cap_sessions_are_never_moved_even_with_a_healthier_provider_available():
    """The leave-in-place rule: a move costs a cache reset, so nothing moves
    merely because some other provider looks emptier."""
    fleet = sessions(("a", "ollama-cloud"), ("b", "ollama-cloud"),
                     ("c", "commandcode"), ("d", "opencode-go"))
    # commandcode is the emptiest provider, and still nothing should move.
    quotas = healthy_quotas()
    quotas["commandcode"] = healthy("commandcode", 0.0)
    result = pl.plan(fleet, quotas, CAPS, PROVIDERS, ALIAS)

    assert moved(result) == [], [a.reason for a in moved(result)]
    assert all(a.keep for a in result)
    assert dest_counts(result) == {"commandcode": 1, "ollama-cloud": 2, "opencode-go": 1}


def test_no_declared_cap_means_no_provider_is_ever_over_one():
    """Caps are what justify a move; with none declared, load alone must not."""
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(9)])
    result = pl.plan(fleet, healthy_quotas(), {}, PROVIDERS, ALIAS)

    assert moved(result) == [], [a.reason for a in moved(result)]
    assert dest_counts(result) == {"ollama-cloud": 9}


def test_a_cap_that_cannot_be_read_is_refused_not_treated_as_unlimited():
    """A mistyped cap used to read as "no cap", so a provider whose real limit was
    3 was filled to 6. The planner cannot honour its main guarantee without knowing
    the limit, so a cap it cannot read stops the plan instead."""
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(6)])
    quotas = healthy_quotas()
    for bad in ("three", {"limit": 3}, [3], True):
        try:
            pl.plan(fleet, quotas, {"ollama-cloud": bad}, PROVIDERS, ALIAS)
        except pl.CapError as exc:
            assert "ollama-cloud" in str(exc), exc
        else:
            raise AssertionError(f"cap {bad!r} was accepted as unlimited")
    # Absent and 0 keep meaning "no declared cap", which is a real configuration.
    assert pl._cap_problems({"ollama-cloud": 0, "clinepass": None}) == []
    for caps in ({}, {"ollama-cloud": 3}, {"ollama-cloud": "3"}):
        assert pl._cap_problems(caps) == [], caps
        pl.plan(fleet, quotas, caps, PROVIDERS, ALIAS)


def test_a_malformed_cap_stops_the_cli_with_a_message():
    """The timer runs this unattended, so the message has to be in the output even
    when the plan is refused."""
    import io
    import tempfile
    from contextlib import redirect_stderr

    store = Path(tempfile.mkdtemp(prefix="ds-cap-")) / "state.db"
    con = sqlite3.connect(store)
    con.execute("create table sessions (id text primary key, session_key text, "
                "billing_provider text, model_config text, title text, model text, "
                "last_activity_at real, ended_at real)")
    con.execute("create table session_turn_leases (conversation_id text, holder text, "
                "acquired_at real, expires_at real)")
    con.commit()
    con.close()
    cfg = Path(tempfile.mkdtemp(prefix="ds-cap-cfg-")) / "config.yaml"
    cfg.write_text(
        "default_model: ds\n"
        "routing:\n"
        "  concurrency:\n"
        "    caps:\n"
        "      ollama-cloud: three\n"
        "providers:\n"
        + "".join(f"  {name}:\n    base_url: https://x/v1\n    key_env: K\n"
                 for name in PROVIDERS)
        + "models:\n  ds:\n" + "".join(f"    {name}: {name}-ds\n" for name in PROVIDERS))
    real_config = pl.CONFIG
    pl.CONFIG = cfg
    stderr = io.StringIO()
    try:
        with redirect_stderr(stderr):
            code = pl.main(["--db", "--db-path", str(store), "--plan"])
    finally:
        pl.CONFIG = real_config
    assert code == 7, (code, stderr.getvalue())
    assert "unreadable concurrency cap" in stderr.getvalue(), stderr.getvalue()
    assert "ollama-cloud" in stderr.getvalue(), stderr.getvalue()


def test_a_scalar_caps_block_is_diagnosed_instead_of_crashing():
    """The README says `caps: 3` is diagnosed. It raised TypeError out of `dict(3)`."""
    import io

    from contextlib import redirect_stderr

    cfg = Path(tempfile.mkdtemp(prefix="ds-scalar-cap-")) / "config.yaml"
    cfg.write_text(
        "default_model: ds\n"
        "routing:\n"
        "  concurrency:\n"
        "    caps: 3\n"
        "providers:\n"
        "  ollama-cloud:\n"
        "    base_url: https://x/v1\n"
        "    key_env: K\n"
        "models:\n"
        "  ds:\n"
        "    ollama-cloud: ollama-cloud-ds\n")
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        code = pl.main(["--plan", "--db", "--config", str(cfg)])
    assert code == 7, (code, stderr.getvalue())
    assert "unreadable concurrency cap" in stderr.getvalue(), stderr.getvalue()
    assert "must map a provider to a limit" in stderr.getvalue(), stderr.getvalue()


def test_a_partial_provider_join_does_not_move_what_it_could_not_read():
    """The all-or-nothing guard has a narrower hole. When SOME sessions resolve and
    others do not, the unresolved ones look exactly like sessions that never had a
    provider, which the planner treats as a reason to move them."""
    class T:
        def call(self, method, params):
            return {"sessions": [
                {"id": "AAA111", "session_key": "k.a", "model": "deepseek/deepseek-v4.1-flash",
                 "title": "a"},
                {"id": "BBB222", "session_key": "k.b", "model": "deepseek/deepseek-v4.1-flash",
                 "title": "b"},
            ]}

    import io
    from contextlib import redirect_stderr

    real = pl._read_db
    pl._read_db = lambda db_path=None: {"k.a": "commandcode"}
    stderr = io.StringIO()
    try:
        with redirect_stderr(stderr):
            fleet = pl.enumerate_sessions(T())
    finally:
        pl._read_db = real
    by_id = {s.id: s for s in fleet}
    assert by_id["AAA111"].provider == "commandcode"
    assert by_id["BBB222"].provider == "" and by_id["BBB222"].provider_known is False, by_id["BBB222"]
    assert "no readable provider" in stderr.getvalue(), stderr.getvalue()

    quotas = healthy_quotas()
    assignments = pl.plan(fleet, quotas, {}, PROVIDERS, ALIAS)
    for a in assignments:
        assert a.keep, (a.session_id, a.reason)
    unread = [a for a in assignments if a.session_id == "BBB222"][0]
    assert "could not be read" in unread.reason, unread.reason
    # A session that genuinely has no provider (not a partial join) is still placed,
    # which is the case this must not break.
    fresh = pl.Session(id="CCC333", provider="", model="deepseek/deepseek-v4.1-flash")
    placed = pl.plan([fresh], quotas, {}, PROVIDERS, ALIAS)[0]
    assert not placed.keep and placed.provider, placed.reason


def test_the_sticky_hold_is_bounded_by_a_real_probe():
    """A provider whose reading failed is held rather than abandoned, which is only
    safe if something can still evict one that is genuinely gone. Hard exhaustion
    needs a reading, and a reading is what is missing, so apply.py asks the router to
    probe the sticky provider in exactly that case: one request, only while its
    reading is bad."""
    import io
    import json
    from contextlib import redirect_stdout
    import apply as A
    import load as q_load
    import router as R

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return type("P", (), {"returncode": 0, "stdout": json.dumps({"chosen": "x"}), "stderr": ""})()

    real_run = A.subprocess.run
    A.subprocess.run = fake_run
    try:
        A.router_decision("clinepass", "ds")
    finally:
        A.subprocess.run = real_run
    assert calls, calls
    assert "--sticky" in calls[0] and "clinepass" in calls[0], calls[0]
    assert "--verify-sticky" in calls[0], calls[0]
    # With no sticky provider there is nothing to hold, so no probe is asked for.
    calls.clear()
    A.subprocess.run = fake_run
    try:
        A.router_decision(None, "ds")
    finally:
        A.subprocess.run = real_run
    assert "--verify-sticky" not in calls[0], calls[0]

    # And the router only probes when the reading really is unusable.
    probed: list[str] = []
    real_ping, real_collect, real_load_by, real_config = (
        R.ping, R.collect, q_load.active_by_provider, R.load_config)
    # main() reads providers from config.yaml; give it the two names this test uses.
    R.load_config = lambda: {
        "default_model": "ds",
        "providers": {"healthy": {"base_url": "https://x/v1", "key_env": "K"},
                      "broken": {"base_url": "https://x/v1", "key_env": "K"}},
        "models": {"ds": {"healthy": "m", "broken": "m"}},
        "routing": {}}
    R.ping = lambda spec, key, model_id, timeout=40.0, max_tokens=64: (
        probed.append("ping"), (False, 0.1, "connection refused"))[1]
    R.collect = lambda providers, env, routing_cfg=None: {
        "healthy": q.Quota("healthy", [win("monthly", 0.1)]),
        "broken": q.Quota("broken", [], 0.0, "HTTPError: 401"),
    }
    q_load.active_by_provider = lambda: q_load.Load({}, "none")
    real_argv = sys.argv
    for sticky, expected in (("broken", 1), ("healthy", 0)):
        probed.clear()
        sys.argv = ["router.py", "--dry-run", "--verify-sticky", "--sticky", sticky, "--json"]
        stdout = io.StringIO()
        try:
            with redirect_stdout(stdout):
                R.main()
        finally:
            sys.argv = real_argv
        assert len(probed) == expected, (sticky, probed)
        payload = json.loads(stdout.getvalue())
        if sticky == "broken":
            # The probe failed, so the provider is hard: it can no longer be held.
            broken = [c for c in payload["candidates"] if c["provider"] == "broken"][0]
            assert broken["hard"] is True, broken
            assert payload["chosen"] != "broken", payload["chosen"]
    R.ping, R.collect, q_load.active_by_provider, R.load_config = (
        real_ping, real_collect, real_load_by, real_config)


def test_a_partial_apply_reports_the_counts_and_exits_nonzero():
    """Three of five moves succeeding must not read as a clean run, and every
    session must still be attempted."""
    import io
    from contextlib import redirect_stdout
    import tempfile

    fleet = [pl.Session(id=f"s{i}", provider="ollama-cloud", model="ollama-cloud-m")
             for i in range(5)]
    cfg = Path(tempfile.mkdtemp(prefix="ds-apply-cfg-")) / "config.yaml"
    cfg.write_text(
        "default_model: " + ALIAS + "\n"
        "routing:\n  concurrency:\n    caps:\n      ollama-cloud: 3\n"
        "providers:\n"
        + "".join(f"  {name}:\n    base_url: https://x/v1\n    key_env: K\n"
                 for name in PROVIDERS)
        + f"models:\n  {ALIAS}:\n" + "".join(f"    {name}: {name}-m\n" for name in PROVIDERS))

    transport = pl.FakeTransport(fail={"s3"})
    # main() prints which backend it read the fleet from, so the stand-in needs one.
    import types
    import load as q_load
    transport.backend = types.SimpleNamespace(describe=lambda: "test backend")
    real = (pl.LiveTransport, pl.enumerate_sessions, pl.collect_quotas, pl.CONFIG,
            q_load.active_by_provider)
    # A hermetic load reading: the real one would make this depend on the machine.
    q_load.active_by_provider = lambda **kwargs: q_load.Load({}, "none")
    pl.CONFIG = cfg
    pl.LiveTransport = lambda **kwargs: transport          # type: ignore[assignment]
    pl.enumerate_sessions = lambda transport=None, **kwargs: fleet  # type: ignore[assignment]
    pl.collect_quotas = lambda providers, config: healthy_quotas()   # type: ignore[assignment]
    stdout = io.StringIO()
    try:
        with redirect_stdout(stdout):
            code = pl.main(["--apply"])
    finally:
        (pl.LiveTransport, pl.enumerate_sessions, pl.collect_quotas, pl.CONFIG,
         q_load.active_by_provider) = real
    out = stdout.getvalue()
    assert code == 1, (code, out)
    assert "FAILED s3" in out, out
    assert "failed 1" in out, out
    assert len([sid for sid in transport.session_ids()]) == 2, transport.session_ids()


def test_the_cli_hands_the_concurrency_reading_to_the_planner():
    """The README promises that sessions already running outside this plan still
    occupy a slot. That held for library callers only: main() passed quotas alone,
    so every provider started on zero load and a cap that was already full could be
    filled again."""
    import tempfile

    store = Path(tempfile.mkdtemp(prefix="ds-cli-load-")) / "state.db"
    now = time.time()
    con = sqlite3.connect(store)
    con.execute("create table sessions (id text primary key, session_key text, "
                "billing_provider text, model_config text, title text, model text, "
                "last_activity_at real, ended_at real)")
    con.execute("create table session_turn_leases (conversation_id text, holder text, "
                "acquired_at real, expires_at real)")
    # A session already running on ollama-cloud: not in the plan (it is archived),
    # but it holds one of the three concurrent slots.
    con.execute("insert into sessions values (?,?,?,?,?,?,?,?)",
                ("busy", "busy-key", "ollama-cloud", "", "busy", "ds", now, None))
    con.execute("insert into session_turn_leases values (?,?,?,?)",
                ("busy-key", "h", now, now + 600))
    con.commit()
    con.close()

    cfg = Path(tempfile.mkdtemp(prefix="ds-cli-cfg-")) / "config.yaml"
    cfg.write_text(
        "default_model: ds\n"
        "routing:\n  concurrency:\n    caps:\n      ollama-cloud: 3\n"
        "providers:\n"
        + "".join(f"  {name}:\n    base_url: https://x/v1\n    key_env: K\n"
                 for name in PROVIDERS)
        + "models:\n  ds:\n" + "".join(f"    {name}: {name}-ds\n" for name in PROVIDERS))

    seen: dict = {}
    real_plan, real_collect, real_config = pl.plan, pl.collect_quotas, pl.CONFIG
    pl.CONFIG = cfg
    pl.collect_quotas = lambda providers, config: healthy_quotas()

    def spy(fleet, readings, caps, providers, alias, **kwargs):
        seen["readings"] = readings
        seen["caps"] = caps
        return real_plan(fleet, readings, caps, providers, alias, **kwargs)

    pl.plan = spy
    try:
        code = pl.main(["--db", "--db-path", str(store), "--plan"])
    finally:
        pl.plan, pl.collect_quotas, pl.CONFIG = real_plan, real_collect, real_config
    assert code == 0, code
    assert seen["readings"]["ollama-cloud"]["load"] == 1, seen["readings"].get("ollama-cloud")
    assert seen["readings"]["ollama-cloud"].get("quota") is not None, "quotas were dropped"
    assert seen["caps"] == {"ollama-cloud": 3}, seen["caps"]


def test_a_draining_provider_is_passed_over_while_a_healthier_one_has_room():
    """Population first, but not onto a provider that is about to throttle.

    opencode-go is the emptiest destination here, so load-first ranking alone
    would send the overflow to it while its weekly window sits at 98%. clinepass
    has room and headroom, so it takes the session instead.
    """
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(4)],
                     *[(f"cp{i}", "clinepass") for i in range(5)],
                     *[(f"cc{i}", "commandcode") for i in range(6)])
    quotas = healthy_quotas()
    quotas["opencode-go"] = q.Quota("opencode-go", [
        win("weekly", 0.98, resets_in=2 * 86400),
        win("session", 0.0, resets_in=3600),
    ])
    result = pl.plan(fleet, quotas, CAPS, PROVIDERS, ALIAS)

    assert len(moved(result)) == 1, [a.reason for a in moved(result)]
    assert moved(result)[0].provider == "clinepass", moved(result)[0].reason
    assert dest_counts(result).get("opencode-go", 0) == 0, dest_counts(result)


def test_a_provider_burning_toward_its_reset_is_passed_over_too():
    """Half of a weekly window is not headroom when it is climbing this fast."""
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(4)],
                     *[(f"cp{i}", "clinepass") for i in range(5)],
                     *[(f"cc{i}", "commandcode") for i in range(6)])
    quotas = healthy_quotas()
    # 50% used with six of the seven days gone: on pace. One day elapsed instead
    # means a 3.3x burn, which reaches 100% long before the window refills.
    quotas["opencode-go"] = q.Quota("opencode-go", [win("weekly", 0.50, resets_in=6 * 86400)])
    result = pl.plan(fleet, quotas, CAPS, PROVIDERS, ALIAS)

    assert moved(result)[0].provider == "clinepass", moved(result)[0].reason


def test_a_window_that_already_reset_is_not_a_reason_to_pass_a_provider_over():
    """A spent window with a past reset time is a stale reading, not a drain."""
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(4)],
                     *[(f"cp{i}", "clinepass") for i in range(5)],
                     *[(f"cc{i}", "commandcode") for i in range(6)])
    quotas = healthy_quotas()
    # 99% and 100% are both ignored: upstream is known to have refilled.
    quotas["opencode-go"] = q.Quota("opencode-go", [
        win("weekly", 0.99, resets_in=-60),
        win("session", 1.0, resets_in=-3600),
    ])
    result = pl.plan(fleet, quotas, CAPS, PROVIDERS, ALIAS)

    # Emptiest provider, nothing draining: it takes the overflow.
    assert moved(result)[0].provider == "opencode-go", moved(result)[0].reason


def test_a_fleet_where_every_destination_is_draining_is_still_placed():
    """Fail open, not refuse: draining providers are a second tier, not a veto."""
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(4)])
    quotas = {name: q.Quota(name, [win("weekly", 0.90, resets_in=2 * 86400)])
              for name in PROVIDERS}
    result = pl.plan(fleet, quotas, CAPS, PROVIDERS, ALIAS)

    assert len(moved(result)) == 1, [a.reason for a in moved(result)]
    assert moved(result)[0].unassigned is False
    assert moved(result)[0].provider not in ("", "ollama-cloud"), moved(result)[0].reason


def test_exhausted_provider_sheds_all_of_its_sessions():
    """Hard exhaustion is not negotiable: every session leaves, even under cap."""
    quotas = healthy_quotas()
    quotas["ollama-cloud"] = q.Quota("ollama-cloud", [win("monthly", 1.0)])
    fleet = sessions(("oc1", "ollama-cloud"), ("oc2", "ollama-cloud"))

    result = pl.plan(fleet, quotas, CAPS, PROVIDERS, ALIAS)

    assert dest_counts(result).get("ollama-cloud", 0) == 0, dest_counts(result)
    assert sorted(a.session_id for a in moved(result)) == ["oc1", "oc2"]
    assert all(a.provider != "ollama-cloud" and "exhausted" in a.reason for a in moved(result))


def test_an_unreadable_provider_takes_no_new_sessions_but_keeps_its_own():
    """A failed READING is not a failed endpoint.

    A provider whose usage endpoint 503s may be serving inference perfectly well,
    so a session already on it must stay: ejecting it costs a prompt-cache reset
    and the model's reasoning traces to avoid a telemetry outage. What an
    unreadable reading does change is that the planner will not put anything new
    there, because it cannot vouch for headroom it could not measure.
    """
    quotas = healthy_quotas()
    quotas["opencode-go"] = q.Quota("opencode-go", [], error="HTTPError: 503")
    fleet = sessions(("a", "opencode-go"), ("b", "commandcode"))

    result = pl.plan(fleet, quotas, CAPS, PROVIDERS, ALIAS)

    assert dest_counts(result).get("opencode-go", 0) == 1, dest_counts(result)
    assert moved(result) == [], [a.reason for a in moved(result)]

    # A session that must be re-homed still must not land on it either.
    providers = dict(PROVIDERS)
    providers["kimi-only"] = {"models": {"kimi-k3": "kimi-k3"}}
    rehomed = pl.plan(sessions(("c", "kimi-only")), quotas,
                      {"ollama-cloud": 3, "kimi-only": 1}, providers, ALIAS)
    assert moved(rehomed), "a session on a provider that cannot serve the alias must move"
    assert dest_counts(rehomed).get("opencode-go", 0) == 0, dest_counts(rehomed)
    assert moved(rehomed)[0].provider != "opencode-go"


def test_a_provider_that_does_not_serve_the_alias_is_never_a_destination():
    """...and a session already sitting on one has to be re-homed."""
    providers = dict(PROVIDERS)
    providers["kimi-only"] = {"models": {"kimi-k3": "kimi-k3"}}
    fleet = sessions(("a", "kimi-only"), ("b", "ollama-cloud"), ("c", "ollama-cloud"))
    caps = {"ollama-cloud": 3, "kimi-only": 1}

    result = pl.plan(fleet, healthy_quotas(), caps, providers, ALIAS)

    assert dest_counts(result).get("kimi-only", 0) == 0, dest_counts(result)
    by_id = {a.session_id: a for a in result}
    assert by_id["a"].changed, "a session on a provider that cannot serve the alias must move"
    assert by_id["b"].keep and by_id["c"].keep
    # Every assignment, keepers included, targets a provider that serves it.
    for a in result:
        if not a.unassigned:
            assert pl._model_id_for(PROVIDERS[a.provider], ALIAS), a.provider


def test_a_session_on_an_unmanaged_provider_is_left_alone():
    """A provider ds-router does not manage is neither capped nor measured, so
    it is neither over cap nor exhausted and a move is not justified."""
    fleet = sessions(("a", "nous"), ("b", "ollama-cloud"), ("c", "ollama-cloud"))
    result = pl.plan(fleet, healthy_quotas(), CAPS, PROVIDERS, ALIAS)

    by_id = {a.session_id: a for a in result}
    assert by_id["a"].keep and not by_id["a"].changed
    assert "not managed" in by_id["a"].reason
    assert by_id["a"].provider == "nous", "must stay exactly where it was"
    assert by_id["b"].keep and by_id["c"].keep


def test_load_that_is_not_in_the_plan_still_occupies_a_slot():
    """Concurrency is in-flight requests, not sessions this script can see: a
    provider already full from outside must shed every session in the plan."""
    fleet = sessions(("a", "ollama-cloud"), ("b", "ollama-cloud"))
    readings = {**healthy_quotas(), "ollama-cloud": {"quota": healthy("ollama-cloud"), "load": 3}}

    result = pl.plan(fleet, readings, CAPS, PROVIDERS, ALIAS)

    assert dest_counts(result).get("ollama-cloud", 0) == 0, dest_counts(result)
    assert len(moved(result)) == 2


def test_a_named_custom_provider_is_read_from_the_config_not_the_billing_identity():
    """Hermes records the runtime identity in billing_provider, which is the
    generic "custom" for every named custom provider, and the durable config key in
    model_config. Reading the billing identity made a clinepass session look like
    an unmanaged provider, so it was never spread and its provider's load was
    undercounted."""
    cfg = json.dumps({"provider": "clinepass", "model": "cline-pass/deepseek-v4.1-flash"})
    assert pl._provider_of("custom", cfg) == "clinepass", pl._provider_of("custom", cfg)
    # The config key wins even when billing names something else.
    assert pl._provider_of("somethingelse", cfg) == "clinepass"
    # Built-in providers carry no model config, so billing is still the source.
    assert pl._provider_of("ollama-cloud", None) == "ollama-cloud"
    assert pl._provider_of("ollama-cloud", "") == "ollama-cloud"
    # Nothing usable anywhere: the generic identity must not leak through.
    assert pl._provider_of("custom", None) == ""
    assert pl._provider_of(None, "not json") == ""


def test_an_unreadable_provider_join_refuses_instead_of_moving_the_fleet():
    """A missing or unreadable state store made every session look provider-less,
    and "no provider yet" is a reason to move, so a one-session plan became a
    whole-fleet rewrite. Verified live: 22/22 sessions to move with a missing DB."""
    class T:
        def call(self, method, params):
            return {"sessions": [{"id": "a", "session_key": "ka"},
                                 {"id": "b", "session_key": "kb"}]}
        def close(self):
            pass

    missing = tmp_path("no-such-dir", "nope.db")
    try:
        pl.enumerate_sessions(T(), db_path=missing)
        raise AssertionError("must refuse when no provider could be read for any session")
    except pl.ProviderJoinError as exc:
        assert "no provider could be read" in str(exc), exc

    # A readable store is read normally. Built here rather than pointing at the
    # developer's own ~/.hermes/state.db, which would make this test pass on one
    # machine and fail on a clean checkout.
    store = tmp_path("state.db")
    con = sqlite3.connect(str(store))
    con.executescript(
        "create table sessions (id text, session_key text, billing_provider text, model_config text);"
    )
    con.execute("insert into sessions values ('a','ka','commandcode',null)")
    con.execute("insert into sessions values ('b','kb','opencode-go',null)")
    con.commit()
    con.close()
    sessions_out = pl.enumerate_sessions(T(), db_path=store)
    assert len(sessions_out) == 2
    assert {s.provider for s in sessions_out} == {"commandcode", "opencode-go"}, sessions_out

    # A store that reads fine but describes OTHER sessions. It is non-empty, so a guard
    # keyed on "did the store hold anything" let every one of these through as
    # provider-less and moved them: that is what a backend on another machine looks like
    # from here, and it is the same whole-fleet rewrite from the other direction.
    foreign = tmp_path("foreign.db")
    con = sqlite3.connect(str(foreign))
    con.executescript(
        "create table sessions (id text, session_key text, billing_provider text, model_config text);"
    )
    con.execute("insert into sessions values ('z','kz','clinepass',null)")
    con.commit()
    con.close()
    try:
        pl.enumerate_sessions(T(), db_path=foreign)
        raise AssertionError("a store that cannot answer for these sessions must refuse")
    except pl.ProviderJoinError as exc:
        assert "no provider could be read" in str(exc), exc
        assert "another machine" in str(exc), exc


def test_apply_reports_a_reply_that_means_nothing_happened():
    """A returned call is not a completed move. Hermes answers config.set with
    confirm_required (its large-context guard, which fires for exactly the
    cross-provider-id moves the planner makes), deferred, or a nested error --
    all of which mean the move did NOT happen."""
    class T:
        def __init__(self, reply):
            self.reply, self.calls = reply, []
        def call(self, method, params):
            self.calls.append((method, params))
            return self.reply
        def close(self):
            pass

    move = [pl.Assignment("s1", "opencode-go", "m2", "ollama-cloud", reason="t")]
    for label, reply in (("deferred", {"deferred": True}),
                         ("nested error", {"error": {"code": 4001, "message": "no live session"}})):
        t = T(reply)
        result = pl.apply(move, t, dry_run=False)[0]
        assert not result.ok, f"{label} must not report success"

    # A blocked move is retried once with the backend's own confirmation flag,
    # because this run is non-interactive and the intent is unambiguous.
    t = T({"confirm_required": True, "confirm_message": "expensive"})
    pl.apply(move, t, dry_run=False)
    assert len(t.calls) == 2, "must retry once with confirmation"
    assert t.calls[1][1].get("confirm_expensive_model") is True, t.calls[1][1]

    # A plain success is still a success. A reply with no result is neither: the
    # backend never said the move happened, so it must not be reported as one.
    for reply in ({"key": "model", "scope": "session"}, "ok"):
        assert pl.apply(move, T(reply), dry_run=False)[0].ok
    for empty in (None, {}, {"result": None}):
        result = pl.apply(move, T(empty), dry_run=False)[0]
        assert not result.ok, f"{empty!r} must not be reported as a completed move"
        assert "confirm" in (result.error or ""), (empty, result.error)


def test_a_failed_quota_read_does_not_eject_sessions_from_a_working_provider():
    """A usage endpoint 503ing says the reading failed, not that inference did."""
    quotas = healthy_quotas()
    quotas["ollama-cloud"] = q.Quota("ollama-cloud", [], error="HTTPError: 503")
    fleet = sessions(("a", "ollama-cloud"), ("b", "ollama-cloud"))

    result = pl.plan(fleet, quotas, CAPS, PROVIDERS, ALIAS)
    assert moved(result) == [], [a.reason for a in moved(result)]


def test_the_alias_is_recognised_by_every_provider_specific_id():
    """A session stores its PROVIDER's id for a model, not the alias. Comparing a
    session's model to the bare alias string therefore marks every session as
    "running something else" and freezes all movement. Caught live: the plan went
    from 5 moves to 0. Every id the alias maps to must be accepted."""
    fleet = [pl.Session("b", "ollama-cloud", "kb", model="deepseek-v4.1-flash"),
             pl.Session("c", "ollama-cloud", "kc", model="deepseek/deepseek-v4.1-flash"),
             pl.Session("d", "ollama-cloud", "kd", model="cline-pass/deepseek-v4.1-flash")]
    quotas = {"ollama-cloud": q.Quota("ollama-cloud", [win("weekly", 0.2, resets_in=3 * 86400)])}

    result = pl.plan(fleet, quotas, {"ollama-cloud": 1}, PROVIDERS, ALIAS)

    assert len(moved(result)) == 2, [a.reason for a in result]
    for a in moved(result):
        assert a.model_id == PROVIDERS[a.provider]["models"][ALIAS], a.model_id


def test_a_session_running_another_model_is_never_repointed():
    """The wire format is '<model-id> --provider <p> --session', so a move sets the
    MODEL as well as the provider. Relocating a session that is deliberately
    running another model would overwrite that choice, so it must be left alone."""
    fleet = [pl.Session("a", "ollama-cloud", "ka", model="kimi-k3"),
             pl.Session("b", "ollama-cloud", "kb", model="kimi-k3")]
    quotas = {"ollama-cloud": q.Quota("ollama-cloud", [win("weekly", 0.1, resets_in=3 * 86400)])}
    providers = {"commandcode": {"models": {ALIAS: "cd/ds"}},
                 "ollama-cloud": {"models": {ALIAS: "ds", "kimi-k3": "kimi-k3"}}}

    result = pl.plan(fleet, quotas, {"ollama-cloud": 1}, providers, ALIAS)

    assert moved(result) == [], [a.reason for a in moved(result)]
    for a in result:
        assert a.model_id == "kimi-k3", a.model_id
        assert "not " + ALIAS in a.reason, a.reason

    # An unknown model is NOT treated as a different one, or nothing would move.
    unknown = [pl.Session("c", "ollama-cloud", "kc", model=""),
               pl.Session("d", "ollama-cloud", "kd", model="")]
    assert moved(pl.plan(unknown, quotas, {"ollama-cloud": 1}, providers, ALIAS))


def test_the_same_input_produces_the_same_plan_twice():
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(7)],
                     *[(f"cc{i}", "commandcode") for i in range(2)],
                     ("zz", "opencode-go"))
    kwargs = dict(skip_at=0.85, weights={"session": 1.0, "weekly": 1.0, "monthly": 0.6})
    first = pl.plan(fleet, healthy_quotas(), CAPS, PROVIDERS, ALIAS, **kwargs)
    second = pl.plan(list(reversed(fleet)), healthy_quotas(), CAPS, PROVIDERS, ALIAS, **kwargs)

    as_tuple = [(a.session_id, a.provider, a.keep, a.reason) for a in first]
    assert as_tuple == [(a.session_id, a.provider, a.keep, a.reason) for a in second]
    assert len(as_tuple) == 10


def test_when_no_provider_can_take_a_session_it_stays_where_it_is():
    """Never strand a session: with nowhere valid to go, leave it alone rather
    than point it at a provider that cannot serve the model."""
    only = {"ollama-cloud": PROVIDERS["ollama-cloud"]}
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(5)])

    result = pl.plan(fleet, healthy_quotas(), CAPS, only, ALIAS)

    assert dest_counts(result) == {"ollama-cloud": 3}, dest_counts(result)
    homeless = [a for a in result if a.unassigned]
    assert len(homeless) == 2, homeless
    assert all(a.provider == "ollama-cloud" and not a.changed for a in homeless)


# ------------------------------------------------------------------- applying


def test_apply_skips_sessions_already_on_their_target_provider():
    fleet = sessions(("keep", "ollama-cloud"), ("move", "ollama-cloud"))
    assignments = pl.plan(fleet, healthy_quotas(), {"opencode-go": 1, "ollama-cloud": 1},
                          PROVIDERS, ALIAS)
    transport = pl.FakeTransport()

    results = pl.apply(assignments, transport, dry_run=False)

    assert [r.session_id for r in results if r.skipped] == ["keep"]
    assert transport.session_ids() == ["move"]
    assert len(transport.calls) == 1


def test_apply_sends_exactly_the_calls_the_plan_implies():
    """The fake records method + params verbatim, so the wire contract is pinned."""
    assignments = [pl.Assignment("s1", "opencode-go", "deepseek-v4.1-flash",
                                 from_provider="ollama-cloud", reason="over cap")]
    transport = pl.FakeTransport(response={"key": "model", "value": "x", "scope": "session"})

    results = pl.apply(assignments, transport, dry_run=False)

    assert transport.calls == [{
        "method": "config.set",
        "params": {"session_id": "s1", "key": "model",
                   "value": "deepseek-v4.1-flash --provider opencode-go --session"},
    }]
    assert results[0].ok and not results[0].skipped and not results[0].dry_run
    assert results[0].provider == "opencode-go"


def test_apply_dry_run_reports_the_calls_and_writes_nothing():
    assignments = [pl.Assignment("s1", "opencode-go", "m", from_provider="ollama-cloud"),
                   pl.Assignment("s2", "opencode-go", "m", from_provider="ollama-cloud")]
    transport = pl.FakeTransport()

    results = pl.apply(assignments, transport, dry_run=True)

    assert transport.calls == [], "a dry run must not touch the transport"
    assert [r.dry_run for r in results] == [True, True]
    assert all(r.params.get("value", "").endswith("--provider opencode-go --session") for r in results)
    assert all(r.ok for r in results)


def test_one_failed_session_does_not_abort_the_rest_of_the_fleet():
    assignments = [pl.Assignment("bad", "opencode-go", "m", from_provider="ollama-cloud"),
                   pl.Assignment("good", "commandcode", "m", from_provider="ollama-cloud")]
    transport = pl.FakeTransport(fail={"bad"})

    results = pl.apply(assignments, transport, dry_run=False)

    assert transport.session_ids() == ["bad", "good"]
    by_id = {r.session_id: r for r in results}
    assert not by_id["bad"].ok and "fake failure" in by_id["bad"].error
    assert by_id["good"].ok


def test_apply_refuses_to_write_with_state_db_ids():
    """Verified against the live backend: config.set with a STORED id (the DB
    fallback's identifier) answers 4001 "requires a live session", so a DB-only
    --apply would fail identically on every session. Refuse up front instead of
    emitting N confusing failures."""
    class T:
        def __init__(self):
            self.calls = []
        def call(self, method, params):
            self.calls.append(method)
            return {"key": "model", "scope": "session"}
        def close(self):
            pass

    move = [pl.Assignment("20260918_065334_28c015", "opencode-go", "m", "ollama-cloud", reason="t")]
    t = T()
    try:
        pl.apply(move, t, dry_run=False, session_ids_are_stored=True)
        raise AssertionError("must refuse to write with stored ids")
    except pl.StoredIdError as exc:
        assert "--db" in str(exc), exc
    assert t.calls == [], "must not send anything"

    # The DB view is still fine to inspect, and dry runs are unaffected.
    assert pl.apply(move, t, dry_run=True, session_ids_are_stored=True)[0].dry_run
    assert pl.apply(move, t, dry_run=False, session_ids_are_stored=False)[0].ok


def test_apply_refuses_a_live_run_with_no_transport():
    assignments = [pl.Assignment("s1", "opencode-go", "m", from_provider="ollama-cloud")]
    try:
        pl.apply(assignments, None, dry_run=False)
    except pl.TransportError as exc:
        assert "transport" in str(exc)
    else:
        raise AssertionError("apply(dry_run=False, transport=None) must raise")


# ------------------------------------------------------- enumeration / transport


def _temp_db(directory: Path) -> Path:
    path = directory / "placement-test-state.db"
    con = sqlite3.connect(str(path))
    con.execute("""create table sessions (
        id text primary key, session_key text, billing_provider text,
        model_config text, model text, title text, source text,
        ended_at real, last_activity_at real, archived integer, hidden integer)""")
    con.executemany("insert into sessions values (?,?,?,?,?,?,?,?,?,?,?)", [
        ("s1", "key-1", "ollama-cloud", '{"provider": "ollama-cloud"}', "m", "one",
         "desktop", None, 100.0, 0, 0),
        ("s2", "key-2", "", '{"model": "m", "provider": "commandcode"}', "m", "two",
         "desktop", None, 90.0, 0, 0),
        ("s3", "key-3", "ollama-cloud", '{"_delegate_from": "key-1"}', "m", "child",
         "subagent", None, 95.0, 0, 0),
        ("s4", "key-4", "opencode-go", "{}", "m", "gone", "desktop", 123.0, 80.0, 0, 0),
    ])
    con.commit()
    con.close()
    return path


def test_enumerate_sessions_prefers_active_list_and_falls_back_to_the_db():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        db = _temp_db(Path(tmp))

        class ActiveList(pl.FakeTransport):
            def call(self, method, params):
                self.calls.append({"method": method, "params": dict(params or {})})
                assert method == "session.active_list"
                return {"sessions": [
                    {"id": "s1", "session_key": "key-1", "model": "m", "title": "one"},
                    {"id": "s2", "session_key": "key-2", "model": "m", "title": "two"},
                ]}

        live = ActiveList()
        rows = pl.enumerate_sessions(live, db_path=db)
        assert [r.id for r in rows] == ["s1", "s2"]
        # The RPC row carries no provider, so it is joined from the DB by key.
        assert {r.id: r.provider for r in rows} == {"s1": "ollama-cloud", "s2": "commandcode"}
        assert live.methods() == ["session.active_list"]

        # No transport -> the DB alone, live human sessions only (no subagent
        # child, no ended session).
        fallback = pl.enumerate_sessions(None, db_path=db)
        assert [r.id for r in fallback] == ["s1", "s2"]
        assert [r.provider for r in fallback] == ["ollama-cloud", "commandcode"]

        # A transport that throws must degrade to the DB, not blow up.
        broken = pl.FakeTransport(fail_methods={"session.active_list"})
        assert [r.id for r in pl.enumerate_sessions(broken, db_path=db)] == ["s1", "s2"]


def test_live_transport_is_not_built_at_import_and_fails_clearly_without_a_backend():
    """Requirement: importing this module must not scan /proc or hold a socket."""
    # The module must hold no transport instance at all: a module-level one would
    # mean importing placement opens a socket as a side effect.
    instances = [name for name, value in vars(pl).items() if isinstance(value, pl.LiveTransport)]
    assert instances == [], f"module-level transport instance(s): {instances}"
    assert pl.LiveTransport  # the class exists; only construction is gated

    original = pl.discover_backends
    pl.discover_backends = lambda: []
    try:
        pl.LiveTransport()
    except pl.TransportError as exc:
        message = str(exc)
        assert "no Hermes backend found" in message
        assert "--port" in message and "--token" in message
    else:
        raise AssertionError("LiveTransport() with no backend must raise TransportError")
    finally:
        pl.discover_backends = original


def test_a_draining_provider_is_passed_over_on_percent_alone():
    """The other draining test's fixture is over its pace line too, so it passes even
    with the percent rule deleted. This one is under pace and only over the line."""
    fleet = sessions(*[(f"oc{i}", "ollama-cloud") for i in range(4)],
                     *[(f"cp{i}", "clinepass") for i in range(5)],
                     *[(f"cc{i}", "commandcode") for i in range(6)])
    quotas = healthy_quotas()
    # 90% of a weekly window with six of its seven days already gone is ON pace
    # (0.86 expected), so only the "at or above the skip point" rule can catch it.
    quotas["opencode-go"] = q.Quota("opencode-go", [win("weekly", 0.90, resets_in=86400)])
    result = pl.plan(fleet, quotas, CAPS, PROVIDERS, ALIAS)
    assert len(moved(result)) == 1, [a.reason for a in moved(result)]
    assert moved(result)[0].provider != "opencode-go", moved(result)[0].reason


def test_an_unreadable_session_store_refuses_instead_of_reporting_no_sessions():
    """--db mapped every read failure to an empty list, so a corrupt store printed
    'sessions : 0' and exited 0, which looks like a clean run."""
    import tempfile
    from pathlib import Path as P
    broken = P(tempfile.mkdtemp(prefix="ds-broken-store-")) / "state.db"
    broken.write_text("this is not a database")
    try:
        pl.sessions_from_db(broken)
    except pl.StoreUnreadable as exc:
        assert "no sessions table" in str(exc) or "cannot open" in str(exc), exc
    else:
        raise AssertionError("an unreadable store must not enumerate as empty")
    # A missing store is a different thing: nothing to enumerate, not a failure.
    absent = P(tempfile.mkdtemp(prefix="ds-absent-store-")) / "state.db"
    assert pl.sessions_from_db(absent) == []


def test_the_cli_reports_the_load_reading_when_it_is_partial():
    """An unattributed session makes the count smaller than reality, and a silent
    undercount looks exactly like a healthy fleet."""
    import io
    import tempfile
    from contextlib import redirect_stderr
    from pathlib import Path as P
    store = P(tempfile.mkdtemp(prefix="ds-load-note-")) / "state.db"
    con = sqlite3.connect(store)
    con.execute("create table sessions (id text primary key, session_key text, "
                "billing_provider text, model_config text, title text, model text, "
                "last_activity_at real, ended_at real)")
    con.execute("create table session_turn_leases (conversation_id text, holder text, "
                "acquired_at real, expires_at real)")
    now = time.time()
    con.execute("insert into sessions values (?,?,?,?,?,?,?,?)",
                ("s1", "k1", "custom", "{not json", "t", "ds", now, None))
    con.execute("insert into session_turn_leases values (?,?,?,?)", ("k1", "h", now, now + 600))
    con.commit()
    con.close()
    cfg = P(tempfile.mkdtemp(prefix="ds-load-note-cfg-")) / "config.yaml"
    cfg.write_text("default_model: ds\nproviders:\n"
                   + "".join(f"  {name}:\n    base_url: https://x/v1\n    key_env: K\n"
                             for name in PROVIDERS)
                   + "models:\n  ds:\n" + "".join(f"    {name}: {name}-m\n" for name in PROVIDERS))
    real_config, real_collect = pl.CONFIG, pl.collect_quotas
    pl.CONFIG = cfg
    pl.collect_quotas = lambda providers, config: healthy_quotas()
    stderr = io.StringIO()
    try:
        with redirect_stderr(stderr):
            code = pl.main(["--db", "--db-path", str(store), "--plan"])
    finally:
        pl.CONFIG, pl.collect_quotas = real_config, real_collect
    assert code == 0, (code, stderr.getvalue())
    assert "no readable provider" in stderr.getvalue(), stderr.getvalue()


def test_the_router_json_names_usage_and_says_whether_health_was_measured():
    """`headroom` was the fullest window's USAGE, so sorting on it picked the worst
    provider, and an empty health map meant 'not probed', not 'all healthy'."""
    import io
    import json
    from contextlib import redirect_stdout
    import load as q_load
    import router as R
    real_collect, real_load = R.collect, q_load.active_by_provider
    R.collect = lambda providers, env, routing_cfg=None: {
        n: q.Quota(n, [win("monthly", 0.2)]) for n in providers}
    q_load.active_by_provider = lambda: q_load.Load({}, "none")
    stdout = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["router.py", "--dry-run", "--json"]
    try:
        with redirect_stdout(stdout):
            code = R.main()
    finally:
        R.collect, q_load.active_by_provider = real_collect, real_load
        sys.argv = old_argv
    assert code == 0, code
    payload = json.loads(stdout.getvalue())
    assert payload["health_measured"] is False, payload["health_measured"]
    assert payload["health"] == {}, payload["health"]
    for candidate in payload["candidates"]:
        assert "usage" in candidate and "headroom" not in candidate, candidate
        assert 0.0 <= candidate["usage"] <= 1.5, candidate


if __name__ == "__main__":
    raise SystemExit(testkit.run(globals()))


