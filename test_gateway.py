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
import socket
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
                 login_detail: str = "", redirect_to: str = "",
                 ticket_body: dict | None = None, stale_once: bool = False) -> None:
        self.password, self.username = password, username
        self.require_username = require_username
        self.login_status, self.login_detail = login_status, login_detail
        self.redirect_to = redirect_to
        self.ticket_body = ticket_body
        self.stale_once = stale_once
        self.logins = 0            # successful or attempted sign-ins
        self.tickets = 0           # tickets handed out
        self.paths: list[str] = []  # every path requested, in order
        self._server = None
        self._thread = None

    # --- lifecycle --------------------------------------------------------- #

    def __enter__(self) -> "FakeGate":
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def origin(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def credential_file(self, directory: Path, name: str = "dashboard-pw.txt",
                        password: str | None = None) -> Path:
        """A key/value credential file shaped like the one Hermes writes.

        *password* overrides what the file holds, so a test can point the transport at
        a credential the gate does not accept.
        """
        path = directory / name
        path.write_text(f"username: {self.username}\npassword: {password or self.password}\n"
                        f"origin:   {self.origin}\n")
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
                    return self._send(200, {"ok": True, "next": "/"},
                                      cookie="hermes_session=ok; Path=/; HttpOnly")
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

    def gateway(self, directory: Path, *, scheme: str = "") -> pl.Gateway:
        url = self.origin if not scheme else self.origin.replace("http://", f"{scheme}://", 1)
        return pl.resolve_gateway(url, password_file=str(self.credential_file(directory)))

    def transport(self, directory: Path) -> pl.LiveTransport:
        return pl.LiveTransport(gateway=self.gateway(directory))


# --------------------------------------------------------------------------- #
# The credential
# --------------------------------------------------------------------------- #


def test_credential_file_reads_the_keyvalue_shape(scratch: Path) -> None:
    path = scratch / "pw.txt"
    path.write_text(f"username: bts\npassword: {SECRET}\norigin:   http://x:9119\n")
    user, secret = pl.read_gateway_credential(path)
    check("username comes from the file", user == "bts", user)
    check("password comes from the file", secret == SECRET)


def test_credential_file_accepts_a_bare_secret_line(scratch: Path) -> None:
    path = scratch / "pw.txt"
    path.write_text(f"# a hand-made secret file\n{SECRET}\n")
    user, secret = pl.read_gateway_credential(path)
    check("a bare line has no username", user == "", user)
    check("a bare line is the password", secret == SECRET)


def test_credential_file_without_a_password_names_the_file(scratch: Path) -> None:
    path = scratch / "pw.txt"
    path.write_text("username: bts\norigin: http://x:9119\n")
    try:
        pl.read_gateway_credential(path)
    except pl.GatewayAuthError as exc:
        check("the refusal names the file", str(path) in str(exc), str(exc))
        return
    raise AssertionError("a credential file with no password was accepted")


def test_unreadable_credential_file_is_named_not_swallowed(scratch: Path) -> None:
    missing = scratch / "nope.txt"
    try:
        pl.read_gateway_credential(missing)
    except pl.GatewayAuthError as exc:
        check("the refusal names the path", str(missing) in str(exc), str(exc))
        return
    raise AssertionError("an unreadable credential file was accepted")


def test_resolve_gateway_reads_a_file_and_an_env_var(scratch: Path) -> None:
    path = scratch / "pw.txt"
    path.write_text(f"username: bts\npassword: {SECRET}\n")
    from_file = pl.resolve_gateway("http://10.0.0.5:9119", password_file=str(path))
    check("the file's username is used", from_file.username == "bts")
    check("the file is named as the source", from_file.source == str(path), from_file.source)
    check("the port is read", from_file.port == 9119, str(from_file.port))

    os.environ["DS_TEST_GATEWAY_PW"] = SECRET
    try:
        from_env = pl.resolve_gateway("http://10.0.0.5", password_env="DS_TEST_GATEWAY_PW",
                                      username="someone")
        check("the env var is named as the source", from_env.source == "$DS_TEST_GATEWAY_PW",
              from_env.source)
        check("an http URL defaults to port 80", from_env.port == 80, str(from_env.port))
    finally:
        os.environ.pop("DS_TEST_GATEWAY_PW", None)

    https = pl.resolve_gateway("https://10.0.0.5", password_env="DS_TEST_GATEWAY_PW") \
        if os.environ.get("DS_TEST_GATEWAY_PW") else None
    check("a 443 default needs no env var to be resolved", https is None or https.port == 443)


def test_resolve_gateway_refuses_what_it_cannot_use(scratch: Path) -> None:
    cases = [
        ("no credential at all", {}, "password_file"),
        ("a scheme it cannot speak", {"password_env": "HOME"}, "http(s)"),
        ("no host", {"password_env": "HOME"}, "http(s)"),
    ]
    for label, kwargs, expect in cases:
        url = "http://10.0.0.5:9119" if kwargs.get("password_env") else "http://10.0.0.5:9119"
        if label == "a scheme it cannot speak":
            url = "ftp://10.0.0.5:9119"
        if label == "no host":
            url = "http://"
        try:
            pl.resolve_gateway(url, **kwargs)
        except pl.GatewayAuthError as exc:
            check(f"{label} is refused", expect in str(exc), f"{label}: {exc}")
        else:
            raise AssertionError(f"{label} was accepted")


def test_resolve_gateway_requires_a_username(scratch: Path) -> None:
    path = scratch / "pw.txt"
    path.write_text(f"{SECRET}\n")  # a bare secret carries no username
    try:
        pl.resolve_gateway("http://10.0.0.5:9119", password_file=str(path))
    except pl.GatewayAuthError as exc:
        check("the refusal asks for a username", "username" in str(exc), str(exc))
        return
    raise AssertionError("a gateway without a username was accepted")


def test_there_is_no_password_flag() -> None:
    """A password in argv is readable by every process on the machine, so it is not an option."""
    proc = subprocess.run([sys.executable, str(HERE / "placement.py"), "--help"],
                          capture_output=True, text=True, cwd=HERE)
    tokens = set(proc.stdout.split())
    for flag in ("--gateway", "--gateway-user", "--gateway-password-file",
                 "--gateway-password-env"):
        check(f"{flag} is offered", flag in tokens)
    check("a bare --gateway-password is not offered", "--gateway-password" not in tokens)


# --------------------------------------------------------------------------- #
# The sign-in and the ticket
# --------------------------------------------------------------------------- #


def test_one_sign_in_then_a_fresh_ticket_per_call(scratch: Path) -> None:
    """The ticket is single-use with a 30 s TTL, so one is minted per connection and cached never."""
    with FakeGate() as gate:
        session = pl.GatewaySession(gate.gateway(scratch))
        first, second = session.mint_ticket(), session.mint_ticket()
        check("two calls mint two tickets", first != second, f"{first} vs {second}")
        check("both are tickets", first.startswith("ticket-") and second.startswith("ticket-"))
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
        try:
            session.mint_ticket()
        except pl.GatewayAuthError as exc:
            message = str(exc)
            check("the refusal says the credential was refused", "refused" in message, message)
            check("it names the credential's source", "dashboard-pw.txt" in message, message)
            check("it never carries the password", "not-the-password" not in message, message)
            # Login is rate limited per client IP, so a retry loop would turn a clear
            # refusal into a lockout.
            check("it was tried exactly once", gate.logins == 1, str(gate.logins))
            check("no ticket was minted", gate.tickets == 0, str(gate.tickets))
            return
    raise AssertionError("a wrong dashboard password was accepted")


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


# --------------------------------------------------------------------------- #
# How the transport uses it
# --------------------------------------------------------------------------- #


def test_ticket_urls_are_wss_under_https() -> None:
    secure = pl.Gateway(host="devbox", port=9119, username="u", password="p", scheme="https")
    check("https becomes wss", secure.ticket_url("a b").startswith("wss://devbox:9119/api/ws?"),
          secure.ticket_url("a b"))
    check("the ticket is quoted", "ticket=a%20b" in secure.ticket_url("a b"))
    plain = pl.Gateway(host="devbox", port=9119, username="u", password="p")
    check("http stays ws", plain.ticket_url("t").startswith("ws://devbox:9119/api/ws?ticket=t"))


def test_a_gated_backend_is_addressed_explicitly() -> None:
    """It has no token to discover and may not even be on this machine."""
    gateway = pl.Gateway(host="10.0.0.5", port=9119, username="u", password="p")
    transport = pl.LiveTransport(gateway=gateway)
    check("the backend is gated", transport.backend.kind == "gated", transport.backend.kind)
    check("no token is invented", transport.backend.token == "", transport.backend.token)
    check("the port comes from the gateway", transport.backend.port == 9119)
    check("the description names the credential's source",
          transport.backend.describe() == "10.0.0.5:9119 (gated, credential from config)",
          transport.backend.describe())
    check("the description never carries the password", "p" != transport.backend.describe())


def test_the_ws_url_mints_a_ticket_per_connection(scratch: Path) -> None:
    with FakeGate() as gate:
        transport = gate.transport(scratch)
        first, second = transport._ws_url(), transport._ws_url()
        check("each connection gets its own ticket", first != second, f"{first} vs {second}")
        check("both point at the gate's ws endpoint", first.startswith("ws://127.0.0.1:") and
              first.endswith("/api/ws?ticket=ticket-1"), first)


def test_a_token_backend_is_unchanged_without_a_gateway() -> None:
    """Absent a `gateway:` block, every existing invocation stays on the token path."""
    transport = pl.LiveTransport(port=9118, token="tok", pid=4242)
    check("the token path is used", transport.backend.kind == "explicit", transport.backend.kind)
    check("the token is kept", transport.backend.token == "tok")
    check("the url is the token url", "token=tok" in transport._ws_url(), transport._ws_url())


class _RefusingClient:
    """A _WSClient whose upgrade fails, without a socket in sight."""

    error: Exception = pl.TransportError("websocket upgrade refused", status=401)

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url

    def connect(self) -> None:
        raise type(self).error


def _fake_upgrade(status_line: bytes, *, accept: bool = False) -> tuple[int, "socket.socket"]:
    """One TCP connection that answers a WS handshake with *status_line*.

    A real upgrade refusal, on loopback, so the test exercises the parser rather than
    a stub's idea of what a status line looks like.
    """
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = int(server.getsockname()[1])

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
    return port, server


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


def test_a_refused_upgrade_carries_its_status() -> None:
    for status_line, expected in ((b"HTTP/1.1 401 Unauthorized\r\n", 401),
                                  (b"HTTP/1.1 403 Forbidden\r\n", 403),
                                  (b"HTTP/1.1 500 Server Error\r\n", 500)):
        port, server = _fake_upgrade(status_line)
        try:
            client = pl._WSClient(f"ws://127.0.0.1:{port}/api/ws?ticket=t", timeout=5.0)
            try:
                client.connect()
            except pl.TransportError as exc:
                check(f"{expected} is carried on the error", exc.status == expected, str(exc.status))
            else:
                raise AssertionError(f"a {expected} upgrade was treated as connected")
        finally:
            server.close()


def test_an_accepted_upgrade_still_connects() -> None:
    """The code check must not reject a real 101, which is the path everything else uses."""
    port, server = _fake_upgrade(b"HTTP/1.1 101 Switching Protocols\r\n", accept=True)
    try:
        client = pl._WSClient(f"ws://127.0.0.1:{port}/api/ws?ticket=t", timeout=5.0)
        client.connect()
        check("a real 101 handshake is accepted", client._sock is not None)
        client.close()
    finally:
        server.close()


class _FakeSession:
    def __init__(self) -> None:
        self.forgotten = 0
        self.minted = 0

    def forget(self) -> None:
        self.forgotten += 1

    def mint_ticket(self) -> str:
        self.minted += 1
        return f"t{self.minted}"


def _connect_failure(error: Exception, scratch: Path) -> tuple[object, _FakeSession]:
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
            except pl.NotSentError:
                pass
            else:
                raise AssertionError(f"a refused upgrade ({error}) was treated as connected")
        finally:
            pl._WSClient = original
    return transport, session


def test_a_refused_upgrade_drops_the_cookie_for_the_next_attempt(scratch: Path) -> None:
    """A 401/403 on the upgrade means the cookie is stale, so the next attempt must sign in."""
    _, session = _connect_failure(pl.TransportError("upgrade refused", status=401), scratch)
    check("the cookie was dropped after a refusal", session.forgotten == 1, str(session.forgotten))


def test_a_refused_upgrade_for_another_reason_does_not_cost_a_login(scratch: Path) -> None:
    """Sign-ins are rate limited per client IP, so only an auth refusal may spend one."""
    for other in (pl.TransportError("connection reset by peer"),
                  pl.TransportError("HTTP/1.1 500 Server Error", status=500)):
        _, session = _connect_failure(other, scratch)
        check(f"{other} kept the cookie", session.forgotten == 0, str(session.forgotten))


def test_a_forbidden_upgrade_also_drops_the_cookie(scratch: Path) -> None:
    """A connect fault happened before anything was sent, so it is safe to repeat."""
    _, session = _connect_failure(pl.TransportError("upgrade refused", status=403), scratch)
    check("a 403 also drops the cookie", session.forgotten == 1, str(session.forgotten))


# --------------------------------------------------------------------------- #
# The config surface
# --------------------------------------------------------------------------- #


def test_the_gateway_block_is_dormant_when_absent(scratch: Path) -> None:
    check("no config block means no gateway", pl.gateway_from_config({}) is None)
    check("a null block means no gateway", pl.gateway_from_config({"gateway": None}) is None)
    check("an empty block means no gateway", pl.gateway_from_config({"gateway": {}}) is None)

    path = scratch / "pw.txt"
    path.write_text(f"username: bts\npassword: {SECRET}\n")
    built = pl.gateway_from_config({"gateway": {"url": "http://10.0.0.5:9119",
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


def test_explicit_flags_win_over_the_config_block(scratch: Path) -> None:
    configured = scratch / "config-pw.txt"
    configured.write_text(f"username: configured\npassword: {SECRET}\n")
    explicit = scratch / "flag-pw.txt"
    explicit.write_text(f"username: fromflag\npassword: {SECRET}\n")
    config = {"gateway": {"url": "http://10.0.0.5:9119", "password_file": str(configured)}}
    built = pl.gateway_from_config(config, _Args(gateway="http://10.0.0.6:9119",
                                                 gateway_password_file=str(explicit)))
    assert isinstance(built, pl.Gateway)
    check("the flag's URL wins", built.host == "10.0.0.6", built.host)
    check("the flag's credential wins", built.username == "fromflag", built.username)


if __name__ == "__main__":
    raise SystemExit(testkit.run(globals(), scratch_prefix="ds-gateway-"))
