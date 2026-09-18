#!/usr/bin/env python3
"""Tests for the JSON-RPC transport: what it retries, and what counts as done.

The suite had no test that constructs the transport at all, and this is the layer
where a state mutation can be sent twice or reported as applied when the backend
said nothing. Both of those are pinned here.

Run: python3 test_transport.py
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import placement as pl
import testkit


def check(label: str, ok: bool, detail: str = "") -> None:
    """A named assertion: raise, so the runner reports the test, the label and the line."""
    if not ok:
        raise AssertionError(label + (f": {detail}" if detail else ""))


class StubClient:
    """Stands in for _WSClient and records exactly what reached the wire."""

    def __init__(self, *, result=None, error=None, null_result=False,
                 connect_error=None, send_error=None, recv_error=None,
                 reply_id_offset: int = 0) -> None:
        self.result, self.error = result, error
        self.null_result = null_result
        self.connect_error, self.send_error, self.recv_error = connect_error, send_error, recv_error
        self.reply_id_offset = reply_id_offset
        self.sent: list[dict] = []
        self.connects = 0
        self.closed = False

    def connect(self) -> None:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error

    def send_text(self, text: str) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(json.loads(text))

    def recv_text(self, timeout=None) -> str:
        if self.recv_error is not None:
            raise self.recv_error
        request_id = (self.sent[-1]["id"] if self.sent else 1) + self.reply_id_offset
        frame: dict = {"jsonrpc": "2.0", "id": request_id}
        if self.null_result:
            frame["result"] = None
        elif self.error is not None:
            frame["error"] = self.error
        else:
            frame["result"] = self.result if self.result is not None else {"scope": "session"}
        return json.dumps(frame)

    def close(self) -> None:
        self.closed = True


def transport_with(clients: list[StubClient]) -> tuple[pl.LiveTransport, StubClient]:
    """A LiveTransport whose connections come from `clients`, in order."""
    built = pl.LiveTransport(port=1, token="t")
    handed: list[StubClient] = []

    def fake_connect() -> StubClient:
        client = clients[min(len(handed), len(clients) - 1)]
        handed.append(client)
        try:
            client.connect()
        except OSError as exc:
            # Mirror the real _connect: a connection failure is a "never sent"
            # fault, which is the only kind that may be repeated.
            raise pl.NotSentError(f"could not reach the backend: {exc}") from exc
        built._ws = client
        return client

    built._connect = fake_connect          # type: ignore[assignment]
    return built, clients[0]


def test_a_good_reply_is_returned() -> None:
    built, client = transport_with([StubClient(result={"scope": "session"})])
    reply = built.call("config.set", {"session_id": "s1"})
    check("the reply is passed through", reply == {"scope": "session"}, str(reply))
    check("exactly one request went out", len(client.sent) == 1, str(client.sent))
    check("the request is well formed JSON-RPC",
          client.sent[0]["method"] == "config.set" and client.sent[0]["params"] == {"session_id": "s1"},
          str(client.sent[0]))


def test_a_server_error_is_final_and_not_repeated() -> None:
    """Repeating an answered error re-sends a mutation the backend already refused
    or already processed, and hides the real error behind a socket fault."""
    built, client = transport_with([StubClient(error={"code": 4001, "message": "no live session"})])
    try:
        built.call("config.set", {"session_id": "s1"})
        raised = None
    except pl.BackendError as exc:
        raised = str(exc)
    check("a JSON-RPC error raises BackendError", raised is not None, str(raised))
    check("the error text is preserved", "4001" in (raised or ""), str(raised))
    check("the request was sent exactly once", len(client.sent) == 1, str(len(client.sent)))


def test_a_timeout_after_the_send_is_not_retried() -> None:
    """The backend may have applied the change before the reply was lost. Retrying
    would apply it twice, and the old code did retry this."""
    client = StubClient(recv_error=socket.timeout("timed out"))
    built, _ = transport_with([client])
    raised: BaseException | None = None
    try:
        built.call("config.set", {"session_id": "s1"})
    except pl.TransportError as exc:
        raised = exc
    check("a post-send timeout raises", raised is not None, str(raised))
    check("it is not classified as 'never sent', so it cannot be repeated",
          not isinstance(raised, pl.NotSentError), type(raised).__name__)
    check("the mutation was sent exactly once", len(client.sent) == 1, str(len(client.sent)))
    check("the socket was dropped afterwards", client.closed, "connection kept open")


def test_a_closed_socket_mid_reply_is_not_retried() -> None:
    client = StubClient(recv_error=ConnectionResetError("connection reset by peer"))
    built, _ = transport_with([client])
    raised = None
    try:
        built.call("config.set", {"session_id": "s1"})
    except pl.TransportError as exc:
        raised = exc
    check("a reset after the send raises", raised is not None, str(raised))
    check("and is not repeated", len(client.sent) == 1, str(len(client.sent)))


def test_a_connection_failure_is_retried_once() -> None:
    """Nothing reached the backend, so repeating it cannot duplicate anything."""
    client = StubClient(connect_error=ConnectionRefusedError("refused"))
    built, _ = transport_with([client])
    raised = None
    try:
        built.call("config.set", {"session_id": "s1"})
    except pl.TransportError as exc:
        raised = exc
    check("a refused connection raises", raised is not None, str(raised))
    check("it is retried exactly once", client.connects == 2, str(client.connects))
    check("nothing was sent", client.sent == [], str(client.sent))


def test_a_failed_send_is_retried_once() -> None:
    client = StubClient(send_error=BrokenPipeError("broken pipe"))
    built, _ = transport_with([client])
    raised = None
    try:
        built.call("config.set", {"session_id": "s1"})
    except pl.TransportError as exc:
        raised = exc
    check("a send failure raises", raised is not None, str(raised))
    check("it is retried once", client.connects == 2, str(client.connects))


def test_a_reply_to_another_request_is_ignored() -> None:
    """Unsolicited frames are normal on a shared socket; the id must match or the
    transport would return somebody else's result."""
    built, client = transport_with([StubClient(result={"scope": "session"}, reply_id_offset=1)])
    try:
        built.call("config.set", {"session_id": "s1"})
        raised = None
    except pl.TransportError as exc:
        raised = str(exc)
    check("an unmatched id is not accepted", raised is not None, str(raised))
    check("it reports no reply", "no reply" in (raised or ""), str(raised))


def test_a_reply_with_no_result_is_unconfirmed_not_moved() -> None:
    """`{"result": null}` means the backend said nothing about the change. It used
    to be reported as a completed move."""
    built, _ = transport_with([StubClient(null_result=True)])
    reply = built.call("config.set", {"session_id": "s1"})
    verdict, detail = pl._read_reply(reply)
    check("a null result is not ok", verdict != "ok", f"{verdict}: {detail}")
    check("it is reported as unconfirmed", verdict == "unconfirmed", verdict)
    check("the message says why", "no result" in detail, detail)


def test_read_reply_classifies_every_shape_it_documents() -> None:
    cases = (
        ({"confirm_required": True, "confirm_message": "expensive"}, "blocked"),
        ({"deferred": True}, "deferred"),
        ({"error": {"code": 7, "message": "nope"}}, "error"),
        ({"error": "plain"}, "error"),
        ({"scope": "session"}, "ok"),
        ("ok", "ok"),
        ({"result": None}, "unconfirmed"),
        (None, "unconfirmed"),
        ({}, "unconfirmed"),
    )
    for reply, expected in cases:
        verdict, detail = pl._read_reply(reply)
        check(f"{str(reply)[:44]!r} -> {expected}", verdict == expected, f"got {verdict}: {detail}")


def test_apply_does_not_count_an_unconfirmed_move_as_done() -> None:
    """The end-to-end version of the same rule: a session whose move the backend
    never confirmed must not be reported as moved or counted as ok."""

    class NullTransport(pl.Transport):
        def __init__(self) -> None:
            self.calls = 0

        def call(self, method: str, params: dict) -> dict:
            self.calls += 1
            return {"result": None}

    assignments = [pl.Assignment(session_id="s1", provider="clinepass", model_id="m",
                                 from_provider="commandcode", reason="over cap")]
    results = pl.apply(assignments, NullTransport(), dry_run=False)
    check("the move is reported as not ok", not results[0].ok, str(results[0]))
    check("the error says it could not be confirmed",
          "confirm" in (results[0].error or ""), str(results[0].error))


def test_apply_refuses_a_write_with_no_transport() -> None:
    assignments = [pl.Assignment(session_id="s1", provider="clinepass", model_id="m",
                                 from_provider="commandcode")]
    raised = None
    try:
        pl.apply(assignments, None, dry_run=False)
    except pl.TransportError as exc:
        raised = str(exc)
    check("a write with no transport is refused", raised is not None, str(raised))
    check("the message suggests the dry run", "dry_run" in (raised or ""), str(raised))


if __name__ == "__main__":
    raise SystemExit(testkit.run(globals(), scratch_prefix="ds-transport-"))
