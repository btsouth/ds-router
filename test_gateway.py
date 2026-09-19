#!/usr/bin/env python3
"""Tests for steering a gated backend: the credential, the sign-in, the ticket.

A backend whose dashboard is published refuses the session token by design, so the
only way in is the way a browser gets in: a dashboard credential, then a single-use
WS ticket. That path holds a password and mints a token that grants control of the
agent, so everything it can get wrong is pinned here.

The backend these run against is a loopback fake, not a real one: the suite stays
hermetic (no `~/.hermes`, no Hermes CLI, nothing off this machine), and every
refusal shape the gate can answer with is produced on demand rather than waited for.

Run: python3 test_gateway.py
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import placement as pl
import testkit

# A fake credential. It is not a secret, and asserting that it never reaches a
# message is the point of several tests below.
SECRET = "f4ke-dashboard-pw"


def check(label: str, ok: bool, detail: str = "") -> None:
    """A named assertion: raise, so the runner reports the test, the label and the line."""
    if not ok:
        raise AssertionError(label + (f": {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# A gated backend on loopback, with every refusal available on demand
# --------------------------------------------------------------------------- #


class FakeGate:
    """The two endpoints the transport uses, and the failures the real ones answer with.

    ``stale_once`` models the cookie going stale upstream: the next ticket request is
    refused once, which is what the transport has to survive without signing in on
    every call.
    """

    def __init__(self, *, password: str = SECRET, username: str = "bts",
                 require_username: bool = True, login_status: int | None = None,
                 login_detail: str = "", login_cookie: bool = True,
                 redirect_to: str = "", ticket_body: dict | None = None,
                 stale_once: bool = False, tls: tuple[Path, Path] | None = None) -> None:
        self.password, self.username = password, username
        self.require_username = require_username
        self.login_status, self.login_detail = login_status, login_detail
        self.login_cookie = login_cookie
        self.redirect_to = redirect_to
        self.ticket_body = ticket_body
        self.stale_once = stale_once
        self.tls = tls
        self.logins = 0            # sign-in attempts
        self.tickets = 0           # tickets handed out
        self.paths: list[str] = []  # every path requested, in order
        self._server = None
        self._thread = None

    # --- lifecycle --------------------------------------------------------- #

    def __enter__(self) -> "FakeGate":
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        if self.tls is not None:
            cert, key = self.tls
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=str(cert), keyfile=str(key))
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def scheme(self) -> str:
        return "https" if self.tls is not None else "http"

    @property
    def origin(self) -> str:
        assert self._server is not None
        return f"{self.scheme}://127.0.0.1:{self._server.server_address[1]}"

    def credential_file(self, directory: Path, name: str = "dashboard-pw.txt",
                        password: str | None = None, mode: int = 0o600,
                        shape: str = "colon", origin: str | None = None) -> Path:
        """A credential file in one of the shapes real ones arrive in.

        * ``colon``: ``label: value``, which is what Hermes' LAN dashboard file holds.
        * ``space``: a title, indented ``label value`` pairs, then prose, which is what
          a hand-written file on this machine holds. Reading the title as the password
          is a real defect this shape pins.
        * ``bare``: one line that is only the secret.
        """
        secret = password or self.password
        where = origin or self.origin
        if shape == "colon":
            text = f"username: {self.username}\npassword: {secret}\norigin:   {where}\n"
        elif shape == "space":
            text = ("Hermes gateway on the audit box\n\n"
                    f"  origin   {where}\n"
                    f"username {self.username}\n"
                    f"password {secret}\n\n"
                    "Type notes here: prose that must never be mistaken for a secret.\n")
        elif shape == "bare":
            text = f"{secret}\n"
        else:
            raise AssertionError(f"unknown credential shape {shape!r}")
        path = directory / name
        path.write_text(text)
        path.chmod(mode)
        return path

    # --- the two endpoints ------------------------------------------------- #

    def _handler(self) -> type:
        gate = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass  # keep the suite quiet

            def _json_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw or b"")
                except ValueError:
                    return {}
                return payload if isinstance(payload, dict) else {}

            def _send(self, status: int, payload: object = None,
                      cookie: str = "", location: str = "") -> None:
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_POST(self) -> None:
                gate.paths.append(self.path)
                body = self._json_body()
                if self.path == "/auth/password-login":
                    gate.logins += 1
                    if gate.redirect_to:
                        # A redirect here means the credential was not accepted; what
                        # matters is that the transport does not follow it.
                        return self._send(302, None, location=gate.redirect_to)
                    if gate.login_status is not None:
                        return self._send(gate.login_status,
                                          {"detail": gate.login_detail or "forced"})
                    accepted = (body.get("password") == gate.password
                                and body.get("provider") == "basic")
                    if gate.require_username:
                        accepted = accepted and body.get("username") == gate.username
                    if not accepted:
                        return self._send(401, {"detail": "Invalid credentials"})
                    cookie = ("hermes_session=ok; Path=/; HttpOnly"
                              if gate.login_cookie else "")
                    return self._send(200, {"ok": True, "next": "/"}, cookie=cookie)
                if self.path == "/api/auth/ws-ticket":
                    cookie = self.headers.get("Cookie") or ""
                    if gate.stale_once:
                        gate.stale_once = False
                        return self._send(401, {"detail": "Unauthorized"})
                    if "hermes_session=ok" not in cookie:
                        return self._send(401, {"detail": "Unauthorized"})
                    if gate.ticket_body is not None:
                        return self._send(200, gate.ticket_body)
                    gate.tickets += 1
                    return self._send(200, {"ticket": f"ticket-{gate.tickets}",
                                            "ttl_seconds": 30})
                return self._send(404, {"detail": "not found"})

            def do_GET(self) -> None:
                gate.paths.append(self.path)
                return self._send(404, {"detail": "not found"})

        return Handler

    # --- the transport, pointed at this gate ------------------------------- #

    def gateway(self, directory: Path, ca_file: str | None = None,
                **kwargs: object) -> pl.Gateway:
        """A Gateway for this server, trusting its own certificate when it is a TLS one."""
        return pl.resolve_gateway(
            self.origin,
            password_file=str(self.credential_file(directory, **kwargs)),  # type: ignore[arg-type]
            ca_file=ca_file or (str(self.tls[0]) if self.tls is not None else None))

    def transport(self, directory: Path) -> pl.LiveTransport:
        return pl.LiveTransport(gateway=self.gateway(directory))


def _make_cert(scratch: Path) -> tuple[Path, Path]:
    """A throwaway self-signed certificate for 127.0.0.1, generated for this run.

    Committing a private key to a public repo is worse than generating one here, and the
    suite takes no dependencies, so this shells out to openssl. That makes openssl a test
    dependency: the TLS checks fail loudly without it rather than passing silently, since
    a skipped check here would be a security-relevant hole reported as green.
    """
    if not shutil.which("openssl"):
        raise AssertionError("openssl is required for the TLS checks in this suite")
    cert, key, conf = scratch / "cert.pem", scratch / "key.pem", scratch / "openssl.cnf"
    conf.write_text(
        "[req]\n"
        "distinguished_name=dn\n"
        "x509_extensions=v3\n"
        "prompt=no\n"
        "[dn]\n"
        "CN=127.0.0.1\n"
        "[v3]\n"
        "subjectAltName=IP:127.0.0.1\n"
        "basicConstraints=critical,CA:TRUE\n")
    proc = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(key), "-out", str(cert), "-config", str(conf)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"openssl could not make a test certificate: {proc.stderr[-200:]}")
    return cert, key


class _ProxyRecorder:
    """A local proxy: it records what it was asked for and answers nothing useful."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                recorder.requests.append(f"{self.command} {self.path} {body}".strip())

            def do_POST(self) -> None:
                self._record()
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self) -> None:
                self._record()
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# The credential file
# --------------------------------------------------------------------------- #


def test_credential_file_reads_the_keyvalue_shape(scratch: Path) -> None:
    path = scratch / "pw.txt"
    path.write_text(f"username: bts\npassword: {SECRET}\norigin:   http://x:9119\n")
    path.chmod(0o600)
    credential = pl.read_gateway_credential(path)
    check("username comes from the file", credential.username == "bts", credential.username)
    check("password comes from the file", credential.password == SECRET)
    check("the origin it names is returned", credential.origins == ("http://x:9119",),
          str(credential.origins))


def test_credential_file_reads_the_space_separated_shape(scratch: Path) -> None:
    """A real file on this machine is a title, indented `label value` pairs, then prose.

    Reading the title line as the password is what the first version did, and it fails
    as "the credential was refused" with prose in the password field.
    """
    with FakeGate() as gate:
        path = gate.credential_file(scratch, shape="space")
        credential = pl.read_gateway_credential(path)
        check("the username line is read", credential.username == "bts", credential.username)
        check("the password line is read, not the title", credential.password == SECRET,
              credential.password)
        check("the prose is not the password", "Hermes gateway" not in credential.password)
        check("the origin it names is returned", credential.origins == (gate.origin,),
              str(credential.origins))


def test_an_annotated_origin_line_yields_only_the_url(scratch: Path) -> None:
    """The real devbox file annotates its second origin on the same line.

    Reading the annotation as part of the URL is not cosmetic: it made the comparison
    raise, and the raise took the whole run down before any credential was sent.
    """
    path = scratch / "pw.txt"
    path.write_text("Hermes gateway on the devbox\n\n"
                    "  origin   https://devbox.example.ts.net\n"
                    "  alt      http://100.64.0.5:9119   (raw tailnet IP, no TLS)\n"
                    "username bts\n"
                    f"password {SECRET}\n")
    path.chmod(0o600)
    credential = pl.read_gateway_credential(path)
    check("the annotated entry keeps only the URL",
          credential.origins == ("https://devbox.example.ts.net", "http://100.64.0.5:9119"),
          str(credential.origins))
    check("an entry that is not a URL is dropped",
          all("://" in entry for entry in credential.origins), str(credential.origins))


def test_the_origin_comparison_can_never_fail_the_run() -> None:
    """It decides whether to print a note, so it has no business raising."""
    cases = [
        ("https://devbox.example.ts.net", "devbox.example.ts.net:443", True),
        ("https://devbox.example.ts.net:443", "devbox.example.ts.net:443", True),
        ("http://100.64.0.5:9119   (raw tailnet IP, no TLS)", "100.64.0.5:9119", False),
        ("not a url at all", "10.0.0.5:9119", False),
        ("", "10.0.0.5:9119", False),
        ("http://10.0.0.5", "10.0.0.5:9119", False),
    ]
    for entry, authority, expected in cases:
        check(f"{entry!r} against {authority}", pl._same_authority(entry, authority) == expected,
              str(pl._same_authority(entry, authority)))


def test_a_label_separated_by_a_colon_with_no_space_is_read(scratch: Path) -> None:
    """`password:secret` is a hand-written file a user will produce."""
    with FakeGate() as gate:
        path = scratch / "tight.txt"
        path.write_text(f"username:{gate.username}\npassword:{SECRET}\n")
        path.chmod(0o600)
        credential = pl.read_gateway_credential(path)
        check("the tight form is read", credential.password == SECRET, credential.password)
        check("and its username too", credential.username == gate.username, credential.username)


def test_a_bare_secret_that_starts_with_a_label_is_still_a_secret(scratch: Path) -> None:
    """An optional separator made "passwd9k2" parse as the label "passwd" with "9k2"."""
    path = scratch / "bare.txt"
    path.write_text("passwd9k2\n")
    path.chmod(0o600)
    credential = pl.read_gateway_credential(path)
    check("the whole line is the secret", credential.password == "passwd9k2",
          credential.password)
    check("no username is invented", credential.username == "", credential.username)


def test_credential_file_accepts_a_bare_secret_line(scratch: Path) -> None:
    path = scratch / "pw.txt"
    path.write_text(f"# a hand-made secret file\n{SECRET}\n")
    path.chmod(0o600)
    credential = pl.read_gateway_credential(path)
    check("a bare line has no username", credential.username == "", credential.username)
    check("a bare line is the password", credential.password == SECRET)


def test_credential_file_without_a_password_names_the_file(scratch: Path) -> None:
    path = scratch / "pw.txt"
    path.write_text("username: bts\norigin: http://x:9119\n")
    try:
        pl.read_gateway_credential(path)
    except pl.GatewayAuthError as exc:
        check("the refusal names the file", str(path) in str(exc), str(exc))
        check("it says what a password line looks like", "password" in str(exc), str(exc))
        return
    raise AssertionError("a credential file with no password was accepted")


def test_a_multi_line_file_that_is_not_a_credential_is_refused(scratch: Path) -> None:
    """Only a SINGLE unlabelled line counts as a bare secret, or prose becomes a password."""
    path = scratch / "notes.txt"
    path.write_text("Some notes about the dashboard\nand a second line of prose\n")
    try:
        pl.read_gateway_credential(path)
    except pl.GatewayAuthError:
        return
    raise AssertionError("a prose file was read as a credential")


def test_unreadable_credential_file_is_named_not_swallowed(scratch: Path) -> None:
    missing = scratch / "nope.txt"
    try:
        pl.read_gateway_credential(missing)
    except pl.GatewayAuthError as exc:
        check("the refusal names the path", str(missing) in str(exc), str(exc))
        return
    raise AssertionError("an unreadable credential file was accepted")


def test_a_credential_file_others_can_read_is_refused(scratch: Path) -> None:
    """The README says keep it at 0600, so reading a 0644 one without a word is the doc lying."""
    with FakeGate() as gate:
        path = gate.credential_file(scratch, mode=0o644)
        try:
            pl.resolve_gateway(gate.origin, password_file=str(path))
        except pl.GatewayAuthError as exc:
            check("the refusal names the mode", "644" in str(exc), str(exc))
            check("it says what to run", "chmod 600" in str(exc), str(exc))
            return
    raise AssertionError("a world-readable credential file was accepted")


def test_a_credential_for_another_origin_is_a_warning_not_a_refusal(scratch: Path) -> None:
    """One file legitimately covers a tailnet name and a LAN address for one backend."""
    with FakeGate() as gate:
        elsewhere = gate.credential_file(scratch, name="elsewhere-pw.txt",
                                         origin="http://10.9.9.9:9119")
        warned: list[str] = []
        gateway = pl.resolve_gateway(gate.origin, password_file=str(elsewhere), warn=warned.append)
        check("the gateway is still built", gateway.host == "127.0.0.1", gateway.host)
        check("the mismatch is reported", len(warned) == 1, str(warned))
        check("the warning names where the password goes", "127.0.0.1" in warned[0], warned[0])
        check("the warning never carries the password", SECRET not in warned[0], warned[0])
        here = gate.credential_file(scratch, name="here-pw.txt")
        matching: list[str] = []
        pl.resolve_gateway(gate.origin, password_file=str(here), warn=matching.append)
        check("a file naming this origin is silent", matching == [], str(matching))


# --------------------------------------------------------------------------- #
# Resolving the origin
# --------------------------------------------------------------------------- #


def test_resolve_gateway_reads_a_file_and_an_env_var(scratch: Path) -> None:
    with FakeGate() as gate:
        path = gate.credential_file(scratch)
        from_file = pl.resolve_gateway(gate.origin, password_file=str(path))
        check("the file's username is used", from_file.username == "bts")
        check("the file is named as the source", from_file.source == str(path), from_file.source)
        check("the port the gate listens on is read", from_file.port > 1024, str(from_file.port))

    os.environ["DS_TEST_GATEWAY_PW"] = SECRET
    try:
        from_env = pl.resolve_gateway("http://10.0.0.5:9119", password_env="DS_TEST_GATEWAY_PW",
                                      username="someone")
        check("the env var is named as the source", from_env.source == "$DS_TEST_GATEWAY_PW",
              from_env.source)
        check("the port is read", from_env.port == 9119, str(from_env.port))
        defaulted = pl.resolve_gateway("http://10.0.0.5", password_env="DS_TEST_GATEWAY_PW",
                                       username="someone")
        check("an http URL defaults to port 80", defaulted.port == 80, str(defaulted.port))
        secure = pl.resolve_gateway("https://10.0.0.5", password_env="DS_TEST_GATEWAY_PW",
                                    username="someone")
        check("an https URL defaults to port 443", secure.port == 443, str(secure.port))
        check("and its upgrade url is wss on that port",
              secure.ticket_url("t") == "wss://10.0.0.5:443/api/ws?ticket=t",
              secure.ticket_url("t"))
    finally:
        os.environ.pop("DS_TEST_GATEWAY_PW", None)


def test_a_shape_the_transport_cannot_speak_is_refused_not_half_supported(scratch: Path) -> None:
    """A URL that looks accepted and fails later is worse than a clear no.

    The userinfo cases matter most: every one of these messages used to interpolate the
    URL as given, so the credential was printed by the very check that rejected it.
    """
    with FakeGate() as gate:
        path = gate.credential_file(scratch)
        cases = [
            ("http://10.0.0.5:9119/dashboard", "prefix", "origin"),
            ("https://10.0.0.5:9119/dashboard", "prefix", "origin"),
            ("http://user@10.0.0.5:9119", "userinfo", "gateway-user"),
            ("http://user:LEAKME@10.0.0.5:9119", "userinfo", "gateway-user"),
            ("https://user:LEAKME@10.0.0.5:9119", "userinfo", "gateway-user"),
            ("http://user:LEAKME@10.0.0.5:9119/dashboard", "userinfo", "gateway-user"),
            ("ftp://user:LEAKME@10.0.0.5:9119", "userinfo", "gateway-user"),
            ("http://0.0.0.0:9119", "wildcard", "127.0.0.1"),
            ("http://[::]:9119", "wildcard", "127.0.0.1"),
            ("http://10.0.0.5:notaport", "not a number", ""),
            ("http://10.0.0.5:0", "port 0", ""),
        ]
        for url, needle, also in cases:
            try:
                pl.resolve_gateway(url, password_file=str(path))
            except pl.GatewayAuthError as exc:
                message = str(exc)
                check(f"{url} is refused", needle in message, message)
                if also:
                    check(f"{url} names the way out", also in message, message)
                check(f"{url} does not echo a password", "LEAKME" not in message, message)
            else:
                raise AssertionError(f"{url} was accepted")


def test_a_ca_file_that_cannot_be_used_is_refused_not_ignored(scratch: Path) -> None:
    """A trust anchor that is silently dropped is worse than none: the user believes it is pinned."""
    with FakeGate() as gate:
        path = gate.credential_file(scratch)
        missing = scratch / "no-such-ca.pem"
        cases = [
            ("https://10.0.0.5:9119", str(missing), "does not exist"),
            ("https://10.0.0.5:9119", str(path), "cannot read the CA certificate"),
            ("http://10.0.0.5:9119", str(missing), "no certificate to check"),
        ]
        for url, ca_file, needle in cases:
            try:
                pl.resolve_gateway(url, password_file=str(path), ca_file=ca_file)
            except pl.GatewayAuthError as exc:
                check(f"ca_file={ca_file} for {url} is refused", needle in str(exc), str(exc))
            else:
                raise AssertionError(f"ca_file={ca_file} for {url} was accepted")


def test_resolve_gateway_refuses_what_it_cannot_use() -> None:
    cases = [
        ("no credential at all", "http://10.0.0.5:9119", {}, "password_file"),
        ("a scheme it cannot speak", "ftp://10.0.0.5:9119", {"password_env": "PATH"}, "http"),
        ("no host", "http://", {"password_env": "PATH"}, "http"),
        ("an env var that is unset", "http://10.0.0.5:9119",
         {"password_env": "DS_TEST_UNSET_VAR"}, "DS_TEST_UNSET_VAR"),
    ]
    for label, url, kwargs, expect in cases:
        try:
            pl.resolve_gateway(url, **kwargs)
        except pl.GatewayAuthError as exc:
            check(f"{label} is refused", expect in str(exc), f"{label}: {exc}")
        else:
            raise AssertionError(f"{label} was accepted")


def test_resolve_gateway_requires_a_username(scratch: Path) -> None:
    path = scratch / "pw.txt"
    path.write_text(f"{SECRET}\n")  # a bare secret carries no username
    path.chmod(0o600)
    try:
        pl.resolve_gateway("http://10.0.0.5:9119", password_file=str(path))
    except pl.GatewayAuthError as exc:
        check("the refusal asks for a username", "username" in str(exc), str(exc))
        return
    raise AssertionError("a gateway without a username was accepted")


def test_two_credential_sources_are_refused_rather_than_one_winning(scratch: Path) -> None:
    with FakeGate() as gate:
        path = gate.credential_file(scratch)
        os.environ["DS_TEST_GATEWAY_PW"] = SECRET
        try:
            pl.resolve_gateway(gate.origin, password_file=str(path),
                               password_env="DS_TEST_GATEWAY_PW")
        except pl.GatewayAuthError as exc:
            check("the refusal names both keys",
                  "password_file" in str(exc) and "password_env" in str(exc), str(exc))
            return
        finally:
            os.environ.pop("DS_TEST_GATEWAY_PW", None)
    raise AssertionError("two credential sources were accepted with one silently winning")


def test_there_is_no_password_flag() -> None:
    """A password in argv is readable by every process on the machine, so it is not an option."""
    proc = subprocess.run([sys.executable, str(HERE / "placement.py"), "--help"],
                          capture_output=True, text=True, cwd=HERE)
    tokens = set(proc.stdout.split())
    for flag in ("--gateway", "--gateway-user", "--gateway-password-file",
                 "--gateway-password-env"):
        check(f"{flag} is offered", flag in tokens)
    check("a bare --gateway-password is not offered", "--gateway-password" not in tokens)


def _run_placement(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / "placement.py"), *args],
                          capture_output=True, text=True, cwd=HERE, timeout=120)


# A config of its own, so these runs do not read the checkout's config.yaml: a user who
# follows the README and adds a `gateway:` block would otherwise change what the suite
# tests, and install.sh's preflight would refuse on their machine.
MINIMAL_CONFIG = (
    "default_model: ds\n"
    "providers:\n"
    "  ollama-cloud:\n"
    "    base_url: https://example.invalid/v1\n"
    "    key_env: DS_TEST_KEY\n"
    "models:\n"
    "  ds:\n"
    "    ollama-cloud: ds-local\n")


def _wrote_config(scratch: Path, text: str = MINIMAL_CONFIG) -> str:
    path = scratch / "config.yaml"
    path.write_text(text)
    return str(path)


def test_no_credential_refuses_rather_than_planning_from_the_store(scratch: Path) -> None:
    """`--gateway` with no credential used to be a silently credential-less run."""
    proc = _run_placement("--plan", "--config", _wrote_config(scratch),
                          "--gateway", "http://127.0.0.1:1")
    check("it refused", proc.returncode == 2, str(proc.returncode))
    check("the reason is a missing credential", "no credential" in proc.stderr,
          proc.stderr[-300:])
    check("no plan was printed", "alias" not in proc.stdout, proc.stdout[-300:])


def test_the_config_path_is_honoured(scratch: Path) -> None:
    """A gateway block in a config this run was pointed at is used; the default is not."""
    with FakeGate() as gate:
        path = gate.credential_file(scratch)
        pointed_at = _wrote_config(scratch, MINIMAL_CONFIG + (
            "gateway:\n"
            f"  url: {gate.origin}\n"
            "  username: bts\n"
            f"  password_file: {path}\n"))
        proc = _run_placement("--plan", "--config", pointed_at)
        check("the pointed-at config was read", "gated, credential from" in proc.stdout
              or "gated, credential from" in proc.stderr, proc.stdout + proc.stderr)
        check("the sign-in used that config's credential", gate.logins == 1, str(gate.logins))


def test_a_credential_failure_refuses_rather_than_planning_from_the_store(scratch: Path) -> None:
    """The failure that must never degrade into a plan.

    A state-DB plan would print with the header still saying "live backend", so the
    user would see a healthy run while steering had quietly stopped working.
    """
    with FakeGate() as gate:
        bad = gate.credential_file(scratch, name="bad-pw.txt", password="not-the-password")
        proc = _run_placement("--plan", "--config", _wrote_config(scratch),
                              "--gateway", gate.origin, "--gateway-user", "bts",
                              "--gateway-password-file", str(bad))
        check("it refused", proc.returncode == 2, str(proc.returncode))
        check("the reason is the refused credential", "refused" in proc.stderr, proc.stderr[-300:])
        check("it says steering stopped", "could not be signed in to" in proc.stderr,
              proc.stderr[-400:])
        check("no plan was printed", "alias" not in proc.stdout, proc.stdout[-300:])
        check("no ticket was minted", gate.tickets == 0, str(gate.tickets))


# --------------------------------------------------------------------------- #
# The sign-in and the ticket
# --------------------------------------------------------------------------- #


def test_one_sign_in_then_a_fresh_ticket_per_call(scratch: Path) -> None:
    """The ticket is single-use with a 30 s TTL, so one is minted per connection and cached never."""
    with FakeGate() as gate:
        session = pl.GatewaySession(gate.gateway(scratch))
        first, second = session.mint_ticket(), session.mint_ticket()
        check("two calls mint two tickets", first != second, f"{first} vs {second}")
        check("both tickets came from the gate", (first, second) == ("ticket-1", "ticket-2"),
              f"{first} / {second}")
        check("the gate handed out two", gate.tickets == 2, str(gate.tickets))
        check("the cookie is reused, not re-obtained", gate.logins == 1, str(gate.logins))
        check("the sign-in happened before the ticket",
              gate.paths == ["/auth/password-login", "/api/auth/ws-ticket",
                             "/api/auth/ws-ticket"], str(gate.paths))


def test_a_wrong_credential_is_refused_and_never_retried(scratch: Path) -> None:
    with FakeGate() as gate:
        # The gate expects SECRET; the file it is handed holds something else.
        wrong = pl.resolve_gateway(
            gate.origin, username="bts",
            password_file=str(gate.credential_file(scratch, password="not-the-password")))
        session = pl.GatewaySession(wrong)
        for attempt in (1, 2):
            try:
                session.mint_ticket()
            except pl.GatewayAuthError as exc:
                message = str(exc)
                check("the refusal says the credential was refused", "refused" in message, message)
                check("it names the credential's source", "dashboard-pw.txt" in message, message)
                check("it never carries the password", "not-the-password" not in message, message)
            else:
                raise AssertionError(f"attempt {attempt}: a wrong dashboard password was accepted")
        # Login is rate limited per client IP, so a second attempt would burn the budget
        # and turn a clear refusal into a lockout.
        check("it was tried exactly once across both attempts", gate.logins == 1,
              str(gate.logins))
        check("no ticket was minted", gate.tickets == 0, str(gate.tickets))


def test_each_login_refusal_shape_says_what_it_is(scratch: Path) -> None:
    cases = [
        (429, "rate limit", "rate limiting"),
        (404, "/api/auth/providers", "no password provider"),
        (503, "credential store", "store unreachable"),
    ]
    for status, needle, label in cases:
        with FakeGate(login_status=status) as gate:
            session = pl.GatewaySession(gate.gateway(scratch))
            try:
                session.mint_ticket()
            except pl.GatewayAuthError as exc:
                check(f"HTTP {status} names the cause ({label})", needle in str(exc), str(exc))
                check(f"HTTP {status} is not retried", gate.logins == 1, str(gate.logins))
            else:
                raise AssertionError(f"HTTP {status} on login was accepted")


def test_a_login_redirect_is_not_followed(scratch: Path) -> None:
    """Following one cannot authenticate anything, and it can put the password on another host."""
    with FakeGate(redirect_to="/login") as gate:
        session = pl.GatewaySession(gate.gateway(scratch))
        try:
            session.mint_ticket()
        except pl.GatewayAuthError:
            check("the redirect target was never fetched", "/login" not in gate.paths,
                  str(gate.paths))
            check("no ticket came out of a redirect", gate.tickets == 0)
            return
    raise AssertionError("a login that answered with a redirect was treated as signed in")


def test_a_200_login_with_no_cookie_is_refused(scratch: Path) -> None:
    """Otherwise the failure surfaces later as a ticket 401 and the real cause is never said."""
    with FakeGate(login_cookie=False) as gate:
        session = pl.GatewaySession(gate.gateway(scratch))
        try:
            session.mint_ticket()
        except pl.GatewayAuthError as exc:
            check("the refusal says no cookie was set", "no session cookie" in str(exc), str(exc))
            check("nothing was ticketed", gate.tickets == 0, str(gate.tickets))
            return
    raise AssertionError("a login that set no cookie was treated as signed in")


def test_a_password_echoed_by_the_server_is_scrubbed(scratch: Path) -> None:
    """The one string in a message that a remote server writes, so it must be cleaned."""
    with FakeGate(login_status=401, login_detail=f"bad password: {SECRET}") as gate:
        session = pl.GatewaySession(gate.gateway(scratch))
        try:
            session.mint_ticket()
        except pl.GatewayAuthError as exc:
            check("the server's detail is kept", "bad password" in str(exc), str(exc))
            check("the password inside it is not", SECRET not in str(exc), str(exc))
            return
    raise AssertionError("a 401 login was accepted")


def test_a_stale_cookie_signs_in_again_exactly_once(scratch: Path) -> None:
    with FakeGate(stale_once=True) as gate:
        session = pl.GatewaySession(gate.gateway(scratch))
        ticket = session.mint_ticket()
        check("the retry produced a ticket", ticket.startswith("ticket-"), ticket)
        check("it signed in twice, no more", gate.logins == 2, str(gate.logins))
        check("the sequence is sign-in, refuse, sign-in, ticket",
              gate.paths == ["/auth/password-login", "/api/auth/ws-ticket",
                             "/auth/password-login", "/api/auth/ws-ticket"], str(gate.paths))


def test_a_ticket_response_without_a_ticket_is_refused(scratch: Path) -> None:
    for body in ({}, {"ticket": ""}, {"ticket": 7}, {"detail": "nope"}):
        with FakeGate(ticket_body=body) as gate:
            session = pl.GatewaySession(gate.gateway(scratch))
            try:
                session.mint_ticket()
            except pl.GatewayAuthError as exc:
                check(f"a {body!r} body is refused", "ticket" in str(exc), str(exc))
            else:
                raise AssertionError(f"a ticket response of {body!r} was accepted")


def test_an_unreachable_gateway_names_the_origin(scratch: Path) -> None:
    gateway = pl.Gateway(host="127.0.0.1", port=1, username="bts", password=SECRET)
    session = pl.GatewaySession(gateway, timeout=2.0)
    try:
        session.mint_ticket()
    except pl.GatewayAuthError as exc:
        check("the refusal names the origin", "127.0.0.1:1" in str(exc), str(exc))
        check("it never carries the password", SECRET not in str(exc))
        return
    raise AssertionError("a gateway that cannot be reached minted a ticket")


def test_the_credential_never_goes_through_an_environment_proxy(scratch: Path) -> None:
    """A login is one POST with a password in the body; an env proxy would receive it whole."""
    with FakeGate() as gate:
        proxy = _ProxyRecorder()
        names = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
        previous = {name: os.environ.get(name) for name in names}
        os.environ["http_proxy"] = proxy.origin
        os.environ["https_proxy"] = proxy.origin
        os.environ["no_proxy"] = ""
        try:
            session = pl.GatewaySession(gate.gateway(scratch))
            ticket = session.mint_ticket()
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            proxy.close()
        check("the proxy was never asked for anything", proxy.requests == [],
              str(proxy.requests))
        check("the gate was reached directly", ticket == "ticket-1", ticket)


# --------------------------------------------------------------------------- #
# How the transport uses it
# --------------------------------------------------------------------------- #


def test_a_gated_backend_is_addressed_explicitly() -> None:
    """It has no token to discover and may not even be on this machine."""
    gateway = pl.Gateway(host="10.0.0.5", port=9119, username="u", password="hunter2")
    transport = pl.LiveTransport(gateway=gateway)
    check("the backend is gated", transport.backend.kind == "gated", transport.backend.kind)
    check("no token is invented", transport.backend.token == "", transport.backend.token)
    check("the port comes from the gateway", transport.backend.port == 9119)
    check("the description names the credential's source",
          transport.backend.describe() == "10.0.0.5:9119 (gated, credential from config)",
          transport.backend.describe())
    check("the description never carries the password",
          "hunter2" not in transport.backend.describe(), transport.backend.describe())
    check("nor does the repr of the gateway", "hunter2" not in repr(gateway), repr(gateway))
    check("the ticket url is a plain ws url with the ticket",
          gateway.ticket_url("t") == "ws://10.0.0.5:9119/api/ws?ticket=t",
          gateway.ticket_url("t"))


def test_a_gateway_refuses_a_host_it_cannot_dial() -> None:
    """The invariants belong to the dataclass, not only to the config path."""
    cases = [
        ("[::1]", "bare literal"),
        ("evil.example@127.0.0.1", "not a host"),
        ("127.0.0.1/dashboard", "not a host"),
        ("0.0.0.0", "wildcard"),
        ("0:0:0:0:0:0:0:0", "wildcard"),
        ("::ffff:0.0.0.0", "wildcard"),
    ]
    for host, needle in cases:
        try:
            pl.Gateway(host=host, port=9119, username="u", password="p")
        except pl.GatewayAuthError as exc:
            check(f"{host} is refused", needle in str(exc), str(exc))
        else:
            raise AssertionError(f"{host} was accepted as a gateway host")


def test_an_ipv6_origin_is_the_same_origin_however_it_is_written() -> None:
    """Otherwise a credential file naming the host it is for produces a false warning."""
    check("bracketed against bracketed", pl._same_authority("https://[::1]:9119", "[::1]:9119"))
    check("unbracketed against bracketed", pl._same_authority("https://[::1]:9119", "[::1]:9119"))
    check("a different port is still different",
          not pl._same_authority("https://[::1]:9119", "[::1]:9120"))


def test_an_ipv6_origin_keeps_its_brackets() -> None:
    """Without them the URL is unparseable, which used to fall back to the state store silently."""
    gateway = pl.Gateway(host="::1", port=9119, username="u", password="p")
    check("the origin brackets the literal", gateway.origin == "http://[::1]:9119",
          gateway.origin)
    check("so does the ticket url",
          gateway.ticket_url("t").startswith("ws://[::1]:9119/api/ws?ticket=t"),
          gateway.ticket_url("t"))
    check("the description brackets it too", gateway.describe().startswith("[::1]:9119"),
          gateway.describe())


def test_ticket_urls_are_wss_under_https() -> None:
    secure = pl.Gateway(host="devbox", port=9119, username="u", password="p", scheme="https")
    check("https becomes wss", secure.ticket_url("a b").startswith("wss://devbox:9119/api/ws?"),
          secure.ticket_url("a b"))
    check("the ticket is quoted", "ticket=a%20b" in secure.ticket_url("a b"))
    plain = pl.Gateway(host="devbox", port=9119, username="u", password="p")
    check("http stays ws", plain.ticket_url("t").startswith("ws://devbox:9119/api/ws?ticket=t"))


def test_a_gateway_refuses_a_ca_file_it_cannot_use() -> None:
    """A pin the code silently dropped would leave the reader believing it took effect."""
    try:
        pl.Gateway(host="h", port=9119, username="u", password="p", ca_file="/tmp/x.pem")
    except pl.GatewayAuthError as exc:
        check("the refusal says https only", "https gateway only" in str(exc), str(exc))
        return
    raise AssertionError("a CA file on a plaintext gateway was accepted")


def test_an_https_gateway_signs_in_and_mints_over_tls(scratch: Path) -> None:
    """What accepting https is for: the password and the ticket never cross in clear."""
    cert, key = _make_cert(scratch)
    with FakeGate(tls=(cert, key)) as gate:
        gateway = gate.gateway(scratch)  # trusts the generated certificate as its CA
        check("https maps to wss", gateway.ticket_url("t").startswith("wss://127.0.0.1:"),
              gateway.ticket_url("t"))
        check("the private CA is named in the description", "private CA" in gateway.describe(),
              gateway.describe())
        session = pl.GatewaySession(gateway, ssl_context=gateway.ssl_context())
        ticket = session.mint_ticket()
        check("a ticket came back over TLS", ticket == "ticket-1", ticket)
        check("the sign-in went over TLS too",
              gate.paths[:2] == ["/auth/password-login", "/api/auth/ws-ticket"], str(gate.paths))


def test_an_untrusted_certificate_stops_the_sign_in(scratch: Path) -> None:
    """No CA file and no system trust: stop, do not hand the password to whoever answered."""
    cert, key = _make_cert(scratch)
    with FakeGate(tls=(cert, key)) as gate:
        untrusted = pl.resolve_gateway(gate.origin,
                                       password_file=str(gate.credential_file(scratch)))
        session = pl.GatewaySession(untrusted, ssl_context=untrusted.ssl_context())
        try:
            session.mint_ticket()
        except pl.GatewayAuthError as exc:
            message = str(exc)
            check("the refusal names the certificate",
                  "could not be verified" in message, message)
            check("it names the way to trust it", "--gateway-ca-file" in message, message)
            # "cannot reach the gateway" sends the reader to fix the credential.
            check("it is not reported as unreachable", "cannot reach" not in message, message)
            check("the server never saw a sign-in", gate.logins == 0, str(gate.logins))
            return
    raise AssertionError("an untrusted certificate was accepted")


def test_the_upgrade_over_tls_verifies_the_certificate(scratch: Path) -> None:
    """The socket that carries the ticket, checked at the handshake rather than trusted."""
    cert, key = _make_cert(scratch)
    with FakeGate(tls=(cert, key)) as gate:
        trusted = gate.gateway(scratch)
        url = trusted.ticket_url("t")
        client = pl._WSClient(url, timeout=5.0, ssl_context=trusted.ssl_context())
        try:
            client.connect()
        except pl.TransportError as exc:
            # This handler answers HTTP, so an HTTP status coming back IS the proof
            # that the TLS handshake succeeded.
            check("the handshake succeeded and an HTTP status came back",
                  exc.status == 404, f"{exc.status}: {exc}")
        else:
            raise AssertionError("a plain HTTP handler upgraded a websocket")

        untrusting = pl._WSClient(url, timeout=5.0, ssl_context=ssl.create_default_context())
        try:
            untrusting.connect()
        except pl.TransportError as exc:
            check("an untrusted certificate is refused",
                  "could not be verified" in str(exc), str(exc))
            check("it is not reported as an HTTP status", exc.status is None, str(exc.status))
            check("the refusal says how to trust it", "ca-file" in str(exc), str(exc))
        else:
            raise AssertionError("an untrusted certificate allowed the upgrade")


def test_the_ws_url_mints_a_ticket_per_connection(scratch: Path) -> None:
    with FakeGate() as gate:
        transport = gate.transport(scratch)
        first, second = transport._ws_url(), transport._ws_url()
        check("each connection gets its own ticket", first != second, f"{first} vs {second}")
        check("each url carries the ticket the gate issued",
              first.endswith("/api/ws?ticket=ticket-1")
              and second.endswith("/api/ws?ticket=ticket-2"), f"{first} {second}")
        check("the gate handed out two", gate.tickets == 2, str(gate.tickets))


def test_a_token_backend_is_unchanged_without_a_gateway() -> None:
    """Absent a `gateway:` block, every existing invocation stays on the token path."""
    transport = pl.LiveTransport(port=9118, token="tok", pid=4242)
    check("the token path is used", transport.backend.kind == "explicit", transport.backend.kind)
    check("the token is kept", transport.backend.token == "tok")
    check("the url is the token url", "token=tok" in transport._ws_url(), transport._ws_url())


def test_a_transport_without_a_gateway_cannot_mint() -> None:
    transport = pl.LiveTransport(port=9118, token="tok", pid=4242)
    try:
        transport._gateway_session()
    except pl.TransportError as exc:
        check("the refusal says no gateway is configured", "no gated gateway" in str(exc), str(exc))
        return
    raise AssertionError("a token transport produced a gateway session")


def test_a_body_that_is_not_json_reads_as_empty() -> None:
    check("an HTML error page reads as empty", pl._json_or_empty(b"<html>nope</html>") == {})
    check("an empty body reads as empty", pl._json_or_empty(b"") == {})
    check("a JSON scalar is returned as itself", pl._json_or_empty(b"7") == 7)


class _RefusingClient:
    """A _WSClient whose upgrade fails, without a socket in sight."""

    error: Exception = pl.TransportError("websocket upgrade refused", status=403)

    def __init__(self, url: str, timeout: float = 10.0,
                 ssl_context: object = None) -> None:
        self.url = url
        self.ssl_context = ssl_context

    def connect(self) -> None:
        raise type(self).error


def _fake_upgrade(status_line: bytes, *, accept: bool = False) -> tuple[int, dict]:
    """One TCP connection that answers a WS handshake with *status_line*.

    A real upgrade refusal, on loopback, so the test exercises the parser rather than
    a stub's idea of what a status line looks like. The request line is recorded so a
    test can assert what was actually asked for.
    """
    seen: dict = {"request": b"", "port": 0}
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    seen["port"] = int(server.getsockname()[1])

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                request += chunk
            seen["request"] = request
            key = ""
            for line in request.decode("latin-1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            headers = b""
            if accept and key:
                digest = base64.b64encode(
                    hashlib.sha1((key + pl._WS_GUID).encode()).digest()).decode()
                headers = f"Sec-WebSocket-Accept: {digest}\r\n".encode()
            conn.sendall(status_line + b"Upgrade: websocket\r\n" + headers + b"\r\n")
            time.sleep(0.05)  # let the client read before the socket closes
        finally:
            conn.close()
            server.close()

    threading.Thread(target=serve, daemon=True).start()
    return seen["port"], seen


def test_the_upgrade_status_is_read_as_a_code_not_a_substring() -> None:
    cases = [
        (b"HTTP/1.1 401 Unauthorized\r\n", 401),
        (b"HTTP/1.1 403 Forbidden\r\n", 403),
        (b"HTTP/2 500 Server Error\r\n", 500),
        (b"not an http response at all\r\n", None),
        (b"", None),
    ]
    for line, expected in cases:
        check(f"{line!r} reads as {expected}", pl._http_status_code(line) == expected,
              str(pl._http_status_code(line)))


def test_a_status_line_is_sanitised_before_it_reaches_a_message() -> None:
    """A peer writes the status line, and the request it answers carried the ticket.

    A refusal is free to reflect that ticket back, or to carry terminal escapes into
    the operator's log and the journal.
    """
    port, _ = _fake_upgrade(b"HTTP/1.1 400 GET /api/ws?ticket=SECRET-TICKET HTTP/1.1\r\n")
    client = pl._WSClient(f"ws://127.0.0.1:{port}/api/ws?ticket=SECRET-TICKET", timeout=5.0)
    try:
        client.connect()
    except pl.TransportError as exc:
        message = str(exc)
        check("the ticket is not reflected into the message", "SECRET-TICKET" not in message,
              message)
        check("it is redacted rather than dropped", "ticket=***" in message, message)
        check("the status itself is still reported", "400" in message, message)
    else:
        raise AssertionError("a 400 upgrade was treated as connected")

    port, _ = _fake_upgrade(b"HTTP/1.1 403 \x1b]0;PWNED\x07\x1b[2Jno\r\n")
    client = pl._WSClient(f"ws://127.0.0.1:{port}/api/ws?ticket=t", timeout=5.0)
    try:
        client.connect()
    except pl.TransportError as exc:
        message = str(exc)
        for escape in ("\x1b", "\x07"):
            check(f"no {escape!r} reaches the message", escape not in message, repr(message))
    else:
        raise AssertionError("a 403 upgrade was treated as connected")


def test_a_status_line_that_never_arrived_says_so() -> None:
    """It used to produce a message ending in "refused: " with nothing after the colon."""
    check("an empty line is named", "no status line" in pl._safe_status_line(b""))
    check("a blank line is named too", "no status line" in pl._safe_status_line(b"\r\n"))
    check("a real line survives", pl._safe_status_line(b"HTTP/1.1 401 Unauthorized\r\n")
          == "HTTP/1.1 401 Unauthorized")


def test_the_host_header_brackets_an_ipv6_literal() -> None:
    """`Host: ::1:9119` is not a valid header, so a strict server refuses the upgrade
    that urllib performs happily on the same authority."""
    check("an IPv6 literal is bracketed", pl._host_header("::1", 9119) == "[::1]:9119",
          pl._host_header("::1", 9119))
    check("a name is not bracketed", pl._host_header("devbox", 9119) == "devbox:9119",
          pl._host_header("devbox", 9119))
    check("an IPv4 literal is not bracketed",
          pl._host_header("127.0.0.1", 80) == "127.0.0.1:80", pl._host_header("127.0.0.1", 80))


def test_a_refused_upgrade_carries_its_status() -> None:
    for status_line, expected in ((b"HTTP/1.1 401 Unauthorized\r\n", 401),
                                  (b"HTTP/1.1 403 Forbidden\r\n", 403),
                                  (b"HTTP/1.1 500 Server Error\r\n", 500)):
        port, _ = _fake_upgrade(status_line)
        client = pl._WSClient(f"ws://127.0.0.1:{port}/api/ws?ticket=t", timeout=5.0)
        try:
            client.connect()
        except pl.TransportError as exc:
            check(f"{expected} is carried on the error", exc.status == expected, str(exc.status))
        else:
            raise AssertionError(f"a {expected} upgrade was treated as connected")


def test_an_accepted_upgrade_carries_the_ticket_and_connects() -> None:
    """The code check must not reject a real 101, and the ticket must be on the request line."""
    port, seen = _fake_upgrade(b"HTTP/1.1 101 Switching Protocols\r\n", accept=True)
    client = pl._WSClient(f"ws://127.0.0.1:{port}/api/ws?ticket=ticket-abc", timeout=5.0)
    client.connect()
    check("a real 101 handshake is accepted", client._sock is not None)
    check("the ticket was on the request line",
          b"GET /api/ws?ticket=ticket-abc" in seen["request"],
          seen["request"][:120].decode("latin-1"))
    client.close()


class _FakeSession:
    def __init__(self) -> None:
        self.forgotten = 0
        self.minted = 0

    def forget(self) -> None:
        self.forgotten += 1

    def mint_ticket(self) -> str:
        self.minted += 1
        return f"t{self.minted}"


def _connect_failure(error: Exception, scratch: Path) -> tuple[Exception, _FakeSession]:
    with FakeGate() as gate:
        transport = gate.transport(scratch)
        session = _FakeSession()
        setattr(transport, "_session", session)
        original = pl._WSClient
        pl._WSClient = _RefusingClient
        _RefusingClient.error = error
        try:
            try:
                transport._connect()
            except pl.NotSentError as exc:
                return exc, session
            raise AssertionError(f"a refused upgrade ({error}) was treated as connected")
        finally:
            pl._WSClient = original
    raise AssertionError("unreachable")


def test_a_refused_upgrade_never_costs_a_sign_in(scratch: Path) -> None:
    """A gate refusal is not evidence about the cookie: 4401, 4403 and 4404 all look like HTTP 403.

    Hermes closes a WS before accept, so the close code never reaches an HTTP client.
    Dropping the cookie on any refusal would spend a rate-limited sign-in on a refusal
    that was never about the credential.
    """
    for status in (401, 403, 500, None):
        error = (pl.TransportError("upgrade refused", status=status) if status
                 else pl.TransportError("connection reset by peer"))
        _, session = _connect_failure(error, scratch)
        check(f"{error} kept the cookie", session.forgotten == 0, str(session.forgotten))


def test_the_upgrade_status_travels_with_the_failure(scratch: Path) -> None:
    exc, _ = _connect_failure(pl.TransportError("upgrade refused", status=403), scratch)
    check("the status survives the not-sent wrapper", getattr(exc, "status", None) == 403,
          str(getattr(exc, "status", None)))


# --------------------------------------------------------------------------- #
# The config surface
# --------------------------------------------------------------------------- #


def test_the_gateway_block_is_dormant_when_absent(scratch: Path) -> None:
    check("no config block means no gateway", pl.gateway_from_config({}) is None)
    check("a null block means no gateway", pl.gateway_from_config({"gateway": None}) is None)
    check("an empty block means no gateway", pl.gateway_from_config({"gateway": {}}) is None)

    with FakeGate() as gate:
        path = gate.credential_file(scratch)
        built = pl.gateway_from_config({"gateway": {"url": gate.origin,
                                                    "password_file": str(path)}})
        check("a configured block builds a gateway", isinstance(built, pl.Gateway))
        assert isinstance(built, pl.Gateway)
        check("the config username is picked up", built.username == "bts", built.username)


def test_a_malformed_gateway_block_refuses_rather_than_ignoring(scratch: Path) -> None:
    try:
        pl.gateway_from_config({"gateway": "http://10.0.0.5:9119"})
    except pl.GatewayAuthError as exc:
        check("the refusal names the block", "gateway" in str(exc), str(exc))
        return
    raise AssertionError("a `gateway:` value that is not a mapping was ignored")


class _Args:
    def __init__(self, **kwargs: object) -> None:
        self.gateway = kwargs.get("gateway")
        self.gateway_user = kwargs.get("gateway_user")
        self.gateway_password_file = kwargs.get("gateway_password_file")
        self.gateway_password_env = kwargs.get("gateway_password_env")
        self.gateway_ca_file = kwargs.get("gateway_ca_file")


def test_the_ca_file_plumbs_through_the_flag_and_the_config(scratch: Path) -> None:
    cert, _ = _make_cert(scratch)
    with FakeGate() as gate:
        path = gate.credential_file(scratch)
        from_config = pl.gateway_from_config({"gateway": {
            "url": "https://10.0.0.5:9119", "password_file": str(path),
            "ca_file": str(cert)}})
        assert isinstance(from_config, pl.Gateway)
        check("the config CA is used", from_config.ca_file == str(cert), str(from_config.ca_file))
        check("the config CA shows in the description", "private CA" in from_config.describe(),
              from_config.describe())
        from_flag = pl.gateway_from_config(
            {"gateway": {"url": "https://10.0.0.5:9119", "password_file": str(path)}},
            _Args(gateway_ca_file=str(cert)))
        assert isinstance(from_flag, pl.Gateway)
        check("the flag's CA is used", from_flag.ca_file == str(cert), str(from_flag.ca_file))
        check("no secret leaks into the repr", "password" not in repr(from_flag), repr(from_flag))


def test_explicit_flags_win_over_the_config_block(scratch: Path) -> None:
    with FakeGate() as gate, FakeGate() as other:
        configured = gate.credential_file(scratch, name="config-pw.txt")
        explicit = other.credential_file(scratch, name="flag-pw.txt")
        config = {"gateway": {"url": gate.origin, "password_file": str(configured)}}
        built = pl.gateway_from_config(config, _Args(gateway=other.origin,
                                                    gateway_password_file=str(explicit)))
        assert isinstance(built, pl.Gateway)
        check("the flag's URL wins", built.port == int(other.origin.rsplit(":", 1)[1]),
              str(built.port))
        check("the flag's credential wins", built.source == str(explicit), built.source)


if __name__ == "__main__":
    raise SystemExit(testkit.run(globals(), scratch_prefix="ds-gateway-"))
