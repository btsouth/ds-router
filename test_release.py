"""Regression cases from the independent pre-release review. No real credentials."""

import contextlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import apply as a
import load
import placement as p
import quota as q
import router
import routing
import test_install
import testkit

SECRET = "review-fake-secret"
HERE = Path(__file__).resolve().parent


@contextlib.contextmanager
def server(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()


class Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_provider_redirect_never_forwards_credentials():
    received = []

    class Sink(Quiet):
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    with server(Sink) as sink:
        class Redirect(Quiet):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{sink.server_port}/capture")
                self.end_headers()

            do_POST = do_GET

        with server(Redirect) as origin:
            url = f"http://127.0.0.1:{origin.server_port}"
            try:
                q._http_json(url, SECRET, 2)
            except Exception:
                pass
            ok, _, _ = router.ping({"base_url": url}, SECRET, "m", timeout=2)
    assert received == [], received
    assert not ok, "redirect is not a successful health probe"


def test_quota_reason_phrase_cannot_leak_key():
    class Reflect(Quiet):
        def do_GET(self):
            self.send_response(401, self.headers.get("Authorization"))
            self.end_headers()

    real = q._http_json
    with server(Reflect) as srv, patch.object(q, "_http_json", side_effect=lambda url, key, timeout:
            real(f"http://127.0.0.1:{srv.server_port}", key, timeout)):
        reading = q.fetch_quota("ollama", {"quota": "ollama"}, SECRET, 2)
    assert "401" in reading.error, reading.error
    assert SECRET not in reading.error, reading.error


def test_switches_serialize_the_whole_transaction(tmp):
    state = dict(zip(a._ROUTED_KEYS, ("old", "old-model", "https://old")))
    first_write = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_done = threading.Event()
    errors = []

    def write(*args):
        _, _, key, value = args
        state[key] = value
        if threading.current_thread().name == "first" and key == "model.provider":
            first_write.set()
            assert release_first.wait(5)

    def switch(name):
        try:
            if name == "second":
                second_started.set()
            a.set_provider(name, {"base_url": f"https://{name}"}, "m", dry=False,
                           models={"m": {"first": "first-model", "second": "second-model"}})
        except Exception as exc:
            errors.append(exc)
        finally:
            if name == "second":
                second_done.set()

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp)}), patch.object(a, "_backup_once"), \
            patch.object(a, "current_value", side_effect=state.__getitem__), \
            patch.object(a, "hermes", side_effect=write):
        first = threading.Thread(target=switch, args=("first",), name="first")
        second = threading.Thread(target=switch, args=("second",), name="second")
        first.start()
        try:
            assert first_write.wait(5)
            second.start()
            assert second_started.wait(5)
            assert not second_done.wait(0.2), "second writer entered an unfinished transaction"
        finally:
            release_first.set()
            first.join(5)
            if second.ident:
                second.join(5)
    assert not errors, errors
    assert state == dict(zip(a._ROUTED_KEYS, ("second", "second-model", "https://second"))), state


def test_missing_endpoint_refuses_before_writing(tmp):
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp)}), patch.object(a, "hermes") as write, \
            patch.object(a, "current_value", return_value="old"), patch.object(a, "_backup_once"):
        try:
            a.set_provider("a", {}, "m", dry=False, models={"m": {"a": "m"}})
        except RuntimeError as exc:
            assert "base_url" in str(exc)
        else:
            raise AssertionError("missing endpoint was accepted")
        assert not write.called


def test_unchanged_config_is_not_rewritten(tmp):
    values = dict(zip(a._ROUTED_KEYS, ("a", "m", "https://a")))
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp)}), patch.object(a, "hermes") as write, \
            patch.object(a, "current_value", side_effect=values.__getitem__), \
            patch.object(a, "_backup_once") as backup:
        a.set_provider("a", {"base_url": "https://a"}, "m", dry=False, models={"m": {"a": "m"}})
    assert not write.called
    assert not (tmp / "config.yaml.bak-ds-router").exists()
    assert not list(tmp.glob(".ds-router-backup-*"))
    assert not backup.called


def test_failed_backup_refuses_the_write(tmp):
    (tmp / "config.yaml").write_text("model: {}\n")
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp)}), \
            patch.object(a.shutil, "copy2", side_effect=PermissionError("denied")), \
            patch.object(a, "current_value", return_value="old"), patch.object(a, "hermes") as write:
        try:
            a.set_provider("a", {"base_url": "https://a"}, "m", dry=False, models={"m": {"a": "m"}})
        except RuntimeError as exc:
            assert "back up" in str(exc)
        else:
            raise AssertionError("routing proceeded without its promised backup")
    assert not write.called
    assert not (tmp / "config.yaml.bak-ds-router").exists()
    assert not list(tmp.glob(".ds-router-backup-*"))


def config():
    return {"default_model": "m", "default_provider": "a",
            "providers": {"a": {"base_url": "https://a", "quota": "ollama"},
                          "b": {"base_url": "https://b", "quota": "ollama"}},
            "models": {"m": {"a": "m", "b": "m"}},
            "routing": {"concurrency": {"caps": {"a": 3, "b": 3}}}}


def store(tmp, n=3):
    path = tmp / "state.db"
    now = time.time()
    with sqlite3.connect(path) as con:
        con.execute("create table sessions(id text, session_key text, billing_provider text, "
                    "model_config text, model text, last_activity_at real, ended_at real)")
        con.execute("create table session_turn_leases(conversation_id text, acquired_at real, expires_at real)")
        for i in range(n):
            con.execute("insert into sessions values(?,?,?,?,?,?,NULL)",
                        (str(i), f"k{i}", "a", "{}", "m", now))
            con.execute("insert into session_turn_leases values(?,?,?)", (f"k{i}", now, now + 300))
    return path


class FakeLive:
    def __init__(self, failure=False):
        self.failure, self.writes = failure, []
        self.backend = type("Backend", (), {"describe": lambda self: "fake"})()

    def call(self, method, params):
        if method == "session.active_list":
            if self.failure:
                raise p.TransportError("simulated listing failure")
            return {"sessions": [{"id": str(i), "session_key": f"k{i}", "model": "m"} for i in range(3)]}
        self.writes.append((method, params))
        return {"scope": "session", "confirm_required": False}


def test_live_fallback_refuses_stored_ids(tmp):
    db = store(tmp)
    transport = FakeLive(failure=True)
    quotas = {"a": q.Quota("a", [q.Window("monthly", 1)]),
              "b": q.Quota("b", [q.Window("monthly", .1)])}
    with patch.object(p, "load_config", return_value=config()), \
            patch.object(p, "LiveTransport", return_value=transport), \
            patch.object(p, "collect_quotas", return_value=quotas), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = p.main(["--apply", "--db-path", str(db)])
    assert code == 6, code
    assert not transport.writes, transport.writes


def test_planned_live_sessions_are_not_external_load(tmp):
    db = store(tmp)
    quotas = {n: q.Quota(n, [q.Window("monthly", .1)]) for n in ("a", "b")}
    import json
    out = io.StringIO()
    with patch.object(p, "load_config", return_value=config()), \
            patch.object(p, "LiveTransport", return_value=FakeLive()), \
            patch.object(p, "collect_quotas", return_value=quotas), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = p.main(["--plan", "--json", "--db-path", str(db)])
    result = json.loads(out.getvalue())
    assert code == 0
    assert all(row["keep"] for row in result["assignments"]), result
    assert result["load"]["readable"], result
    assert result["load"]["counts"] == {}, result


def test_empty_db_fallback_does_not_report_live_success(tmp):
    db = store(tmp, 0)
    for failed in (True, False):
        transport = FakeLive(failure=failed)
        if not failed:
            transport.call = lambda method, params: {"result": None}
        with patch.object(p, "load_config", return_value=config()), \
                patch.object(p, "LiveTransport", return_value=transport), \
                patch.object(p, "collect_quotas", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = p.main(["--apply", "--db-path", str(db)])
        assert code == 6, code
        assert not transport.writes


def test_external_leases_still_hold_slots(tmp):
    db = store(tmp, 4)
    got = load.active_by_provider(session_db=db, exclude_session_ids={"0", "k1", "2"})
    assert got.readable, got
    assert got.counts == {"a": 1}, got


def test_apply_db_refuses_before_backend_discovery():
    with patch.object(p, "LiveTransport", side_effect=AssertionError("must not discover")), \
            contextlib.redirect_stderr(io.StringIO()):
        assert p.main(["--apply", "--db"]) == 6


def test_invalid_config_is_diagnosed_by_all_loaders(tmp):
    path = tmp / "bad.yaml"
    for text in ("providers: [a]\n", "routing: {quota_ttl_seconds: typo}\n",
                 "routing: {concurrency: true}\n", "routing: {skip_at: .nan}\n",
                 "models: {m: {a: [wrong]}}\n", "default_model: 42\n",
                 "providers: {a: {base_url: [wrong]}}\n",
                 'routing: {concurrency: {enabled: "false"}}\n'):
        path.write_text(text)
        for mod in (a, p, router):
            with patch.object(mod, "CONFIG", path):
                try:
                    mod.load_config()
                except mod.ConfigError:
                    pass
                else:
                    raise AssertionError(f"{mod.__name__} accepted {text}")


def test_collector_failure_does_not_become_load_only_placement():
    with patch.object(router, "collect", side_effect=ValueError("simulated defect")):
        try:
            p.collect_quotas({}, {})
        except p.ConfigError as exc:
            assert "quota collection failed" in str(exc)
        else:
            raise AssertionError("collector failure swallowed")


def test_catalog_failures_exit_nonzero():
    with patch.object(router, "load_config", return_value=config()), \
            patch.object(q, "_http_json", side_effect=OSError(SECRET)), \
            patch.object(sys, "argv", ["router.py", "--list-models"]), \
            contextlib.redirect_stdout(io.StringIO()) as out:
        code = router.main()
    assert code == 1, code
    assert SECRET not in out.getvalue()


def test_unmetered_provider_is_not_a_routing_destination():
    quota = q.fetch_quota("a", {"quota": ""}, "")
    result = routing.choose("m", {"a": {"models": {"m": "m"}}}, {"a": quota}, {}, .85)
    assert not result.ok, result
    with patch.object(q, "from_collector", return_value=q.Quota("a", [q.Window("monthly", .1)])):
        collected = router.collect({"a": {"quota": None}}, {},
                                   routing_cfg={"collector_state_dir": "/unused"})
    assert collected["a"].stale, "a snapshot re-enabled a provider with no quota reader"


def test_peak_price_cannot_override_lower_risk():
    providers = {n: {"models": {"m": "m"}} for n in ("a", "b")}
    quotas = {n: q.Quota(n, [q.Window("monthly", percent)]) for n, percent in (("a", .1), ("b", .8))}
    result = routing.choose("m", providers, quotas, {}, .85, peak_providers={"a"})
    assert result.provider == "a", result


def sandbox(tmp):
    home = tmp / "home"
    home.mkdir()
    test_install.fake_hermes(tmp)
    test_install.fake_systemctl(tmp, bus=True)
    return home, test_install.sandbox_env(tmp, home)


def test_installer_dry_run_does_not_follow_predictable_symlink(tmp):
    home, env = sandbox(tmp)
    victim = tmp / "unrelated"
    victim.write_text("PRESERVE ME")
    env["VICTIM"] = str(victim)
    proc = subprocess.run(["sh", "-c",
        'ln -s "$VICTIM" "$TMPDIR/ds-router-install.$$.unit"; '
        'exec ./install.sh --dry-run --skip-preflight'], cwd=HERE, env=env,
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert victim.read_text() == "PRESERVE ME"
    assert not list(home.rglob("*")), list(home.rglob("*"))


def test_uninstaller_stops_but_preserves_modified_units(tmp):
    home, env = sandbox(tmp)
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    timer = units / "ds-router.timer"
    timer.write_text("[Timer]\nOnUnitActiveSec=1min\n")
    proc = subprocess.run([str(HERE / "uninstall.sh"), "--dry-run"], env=env,
                          capture_output=True, text=True, timeout=30)
    assert "disable --now ds-router.timer" in proc.stdout, proc.stdout
    assert "now inert" not in proc.stdout
    assert proc.returncode == 1, proc.returncode
    assert timer.exists()


def test_uninstaller_dry_run_does_not_follow_predictable_symlink(tmp):
    home, env = sandbox(tmp)
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    (units / "ds-router.timer").write_text("[Timer]\nOnUnitActiveSec=1min\n")
    victim = tmp / "unrelated"
    victim.write_text("PRESERVE ME")
    env["VICTIM"] = str(victim)
    subprocess.run(["sh", "-c",
        'ln -s "$VICTIM" "$TMPDIR/ds-router-uninstall.$$.unit"; exec ./uninstall.sh --dry-run'],
        cwd=HERE, env=env, capture_output=True, text=True, timeout=30)
    assert victim.read_text() == "PRESERVE ME"


def test_install_rerun_preserves_file_content_and_mtime(tmp):
    # Only sandbox service commands run. Neither pass contacts a real user manager.
    first = test_install.run_install(tmp, "--skip-preflight", "--yes")
    assert first.returncode == 0, first.stderr
    home = tmp / "home"
    def snapshot():
        return {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in home.rglob("*") if path.is_file() and not path.is_symlink()}
    before = snapshot()
    again = test_install.run_install(tmp, "--skip-preflight", "--yes")
    assert again.returncode == 0, again.stderr
    assert snapshot() == before


if __name__ == "__main__":
    raise SystemExit(testkit.run(globals(), scratch_prefix="ds-release-"))
