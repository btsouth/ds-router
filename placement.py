#!/usr/bin/env python3
"""Per-session provider *placement*: spread N sessions across providers.

`routing.py` picks ONE provider for the whole install. That is not enough when
twelve sessions are open, because they all inherit that one provider and pile
onto it. Ollama Cloud Pro allows 3 concurrent requests, so eight sessions on
Ollama is a queue, and a full queue is a rejection.

Hermes supports a per-session provider override (`config.set {session_id,
key:"model"}` with a `--provider` value, verified live against the desktop
backend), so placement is the real lever: assign each session its own provider
instead of letting them share one.

Three rules carry this module, and they are the same three as `routing.py`:

1. **Leave sessions where they are.** A move resets the upstream prompt cache
   and drops reasoning traces, so it must be justified. A session only moves
   when its provider is over its declared concurrency cap, or exhausted, or
   unreadable.
2. **A cap is a hard limit on the plan, not a scoring nudge.** `routing.py`
   prices a queue overage as soft pressure (queueing still completes). Here the
   plan must not create the queue in the first place.
3. **Never choose a provider that does not serve the model alias**, and never
   move a session onto a provider a reading says is spent.

Everything RPC-shaped goes through `Transport.call(method, params)`, so the
whole planner and applier are testable with `FakeTransport` and no backend.

Usage:

    python3 placement.py --plan            # print the target assignment; writes nothing
    python3 placement.py --plan --json
    python3 placement.py --apply           # apply it (explicit flag required)
    python3 placement.py --apply --dry-run # print exactly what would be sent
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import sqlite3
import struct
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import quota as quota_mod  # noqa: E402
import routing as routing_mod  # noqa: E402

CONFIG = HERE / "config.yaml"


def state_db_path(db_path: Optional[Path] = None) -> Path:
    """Where Hermes' session store lives.

    ``HERMES_HOME`` wins when set, because that is what the desktop backend and
    every profile-scoped process export, and it is the only signal that stays
    correct when the session's ``HOME`` is not the user's (a spawned backend can
    carry a different HOME). Falls back to ``~/.hermes``.
    """
    if db_path is not None:
        return Path(db_path)
    # One resolver for the whole project (src/paths.py).
    from paths import state_db
    return state_db()

# Matches config.yaml's routing defaults. Only used when the caller passes no
# value, so a config change is never silently ignored by this module.
SKIP_AT = 0.85
WEIGHTS = {"session": 1.0, "weekly": 1.0, "monthly": 0.6}

# The RPC the desktop composer itself uses for a per-session provider change.
DEFAULT_METHOD = "config.set"


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


@dataclass
class Session:
    """One live Hermes session and the provider it currently runs on."""

    id: str
    provider: str = ""
    session_key: str = ""
    model: str = ""
    title: str = ""


@dataclass
class Assignment:
    """Where a session should end up, and why.

    ``keep`` is the ordinary case: the session is already on its target and
    nothing should be sent for it. ``unassigned`` marks the rare case where no
    provider could take the session at all — the session is left alone rather
    than pointed at a provider that cannot serve it, because "no provider" is
    worse than the one it already had.
    """

    session_id: str
    provider: str
    model_id: str = ""
    from_provider: str = ""
    keep: bool = False
    reason: str = ""
    unassigned: bool = False

    @property
    def changed(self) -> bool:
        """True only when this assignment requires a transport call."""
        return (not self.keep and not self.unassigned and bool(self.provider)
                and self.provider != self.from_provider)


@dataclass
class Result:
    """What happened (or would happen) for one assignment."""

    session_id: str
    provider: str
    from_provider: str = ""
    ok: bool = True
    skipped: bool = False
    dry_run: bool = False
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    reason: str = ""


# --------------------------------------------------------------------------- #
# Readings: quotas and/or load, in whatever shape the caller has
# --------------------------------------------------------------------------- #


@dataclass
class Readings:
    """Per-provider inputs: a quota reading, an observed load, or neither.

    ``quotas[name] is None`` means *nobody asked*, which is deliberately
    different from a reading that came back stale (``Quota.stale``) — see
    `_Candidate.usable`. Asking a provider's quota API is optional here, while
    a failed request is information and is acted on.
    """

    quotas: dict[str, Optional[quota_mod.Quota]] = field(default_factory=dict)
    load: dict[str, int] = field(default_factory=dict)


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def normalize(quotas_or_load: Any) -> Readings:
    """Accept quotas, a load mapping, a `load.Load`, or a mix of all three.

    Recognised, per provider name:

    * a `quota.Quota`                     -> the quota reading
    * an int                              -> an observed session count
    * ``{"quota": Quota, "load": int}``   -> both
    * a ``load.Load`` (has ``.counts``)   -> counts
    """
    out = Readings()
    if quotas_or_load is None:
        return out
    counts = getattr(quotas_or_load, "counts", None)
    if isinstance(counts, dict):  # load.Load
        quotas_or_load = counts
    if not isinstance(quotas_or_load, dict):
        return out
    for name, value in quotas_or_load.items():
        name = str(name)
        if isinstance(value, quota_mod.Quota):
            out.quotas[name] = value
        elif isinstance(value, dict):
            if isinstance(value.get("quota"), quota_mod.Quota):
                out.quotas[name] = value["quota"]
            as_int = _as_int(value.get("load"))
            if as_int is not None:
                out.load[name] = as_int
        else:
            as_int = _as_int(value)
            if as_int is not None:
                out.load[name] = as_int
    return out


def _model_id_for(spec: Any, alias: str) -> str:
    """The id *spec* calls *alias*, or '' when the provider does not serve it.

    Accepts `router.build_view`'s shape (``{"models": {alias: id}, ...}``) and a
    bare ``{alias: id}`` table.
    """
    if not isinstance(spec, dict) or not alias:
        return ""
    table = spec.get("models")
    if not isinstance(table, dict):
        table = spec
    return str(table.get(alias) or "")


def _positive_cap(value: Any) -> Optional[int]:
    """A cap only counts when it is a positive integer. Absent/0 = unbounded."""
    as_int = _as_int(value)
    return as_int if as_int is not None and as_int > 0 else None


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


@dataclass
class _Candidate:
    """One provider, as a placement destination."""

    name: str
    model_id: str
    cap: Optional[int]
    external: int                      # observed load that is not in this plan
    quota: Optional[quota_mod.Quota]

    @property
    def unreadable(self) -> bool:
        """A reading was taken and failed (or came back empty)."""
        return self.quota is not None and self.quota.stale

    @property
    def exhausted(self) -> bool:
        return (self.quota is not None and not self.quota.stale
                and routing_mod.hard_exhausted(self.quota, SKIP_AT))

    @property
    def usable(self) -> bool:
        return not self.unreadable and not self.exhausted


def _risk(candidate: _Candidate, skip_at: float, weights: dict[str, float],
          assigned: int, now: float) -> float:
    """Exhaustion risk, used only to break a load tie between destinations.

    A provider with no reading at all scores 0.5: it is neither trusted (a
    healthy provider scores near 0) nor refused (a stale one is excluded
    outright). Refusing an unasked provider would strand sessions on a
    saturated one, which is the worse failure.
    """
    if candidate.quota is None:
        return 0.5
    scored = routing_mod.score(candidate.quota, candidate.model_id, weights, skip_at,
                               now=now, active_sessions=candidate.external + assigned,
                               concurrency_cap=candidate.cap)
    return float(scored.pressure)


def _destination(cands: dict[str, _Candidate], assigned: dict[str, int], *,
                 skip_at: float, weights: dict[str, float], now: float) -> Optional[_Candidate]:
    """The best place to put one more session, or None if nowhere can take it.

    Ranked by (load, risk, name). Load first is the whole point — spreading is
    what keeps every provider under its cap — with quota risk as a tie-break so
    two equally loaded providers resolve toward the healthier one. The name
    tiebreak is what makes the result deterministic.
    """
    best: Optional[tuple[tuple, _Candidate]] = None
    for name in sorted(cands):
        cand = cands[name]
        if not cand.usable:
            continue
        load = cand.external + assigned.get(name, 0)
        if cand.cap is not None and load >= cand.cap:
            continue
        key = (load, _risk(cand, skip_at, weights, assigned.get(name, 0), now), name)
        if best is None or key < best[0]:
            best = (key, cand)
    return best[1] if best else None


def _why_leaving(source: str, cands: dict[str, _Candidate], declared: dict[str, Any],
                 alias: str, over_cap: bool) -> str:
    """A one-line justification for moving a session, for the printed table."""
    if not source:
        return "no provider yet"
    cand = cands.get(source)
    if cand is None:
        if source in declared:
            return f"{source} does not serve {alias!r}"
        return f"{source} is not a declared provider"
    if cand.exhausted:
        return f"{source} is exhausted"
    if cand.unreadable:
        return f"{source} quota is unreadable"
    if over_cap:
        return f"{source} is over its concurrency cap"
    return f"{source} cannot take it"


def plan(sessions: Iterable[Any], quotas_or_load: Any = None, caps: Any = None,
         providers: Any = None, alias: str = "", *,
         skip_at: float = SKIP_AT, weights: Optional[dict[str, float]] = None,
         now: Optional[float] = None) -> list[Assignment]:
    """Spread *sessions* across the providers that serve *alias*.

    Guarantees, in order of importance:

    * no provider is assigned more sessions than its cap allows, counting any
      observed load that is not in *sessions* as already occupying a slot;
    * a session stays where it is unless its provider is over cap, exhausted,
      unreadable, undeclared, or does not serve the alias;
    * a provider that does not serve the alias is never a destination;
    * the same input always produces the same output (every ordering in here is
      a sort on names and session ids, never on dict iteration order).

    *quotas_or_load* takes quotas, a load mapping, a `load.Load`, or a mix — see
    `normalize`. Absent readings are "unknown", not "unreadable": see
    `_Candidate.usable`.

    Returns one `Assignment` per session, in a deterministic order.
    """
    weights = dict(weights or WEIGHTS)
    now = time.time() if now is None else now
    declared = dict(providers or {})
    readings = normalize(quotas_or_load)
    rows = [_as_session(s) for s in (sessions or [])]

    cands: dict[str, _Candidate] = {}
    for name in sorted(declared):
        model_id = _model_id_for(declared[name], alias)
        if not model_id:
            continue  # (d) a provider that does not serve the alias is not a candidate
        caps = dict(caps or {})
        cands[name] = _Candidate(name, model_id, _positive_cap(caps.get(name)),
                                 readings.load.get(name, 0), readings.quotas.get(name))

    assigned: dict[str, int] = {name: 0 for name in cands}
    out: list[Assignment] = []
    movers: list[tuple[Session, bool]] = []
    seen: set[str] = set()

    # Phase 1 — who may stay. Sessions are visited provider by provider, and
    # within a provider in id order, so "the first N keep their slot" is stable.
    for name in sorted(cands):
        cand = cands[name]
        room = None if cand.cap is None else max(0, cand.cap - cand.external)
        kept = 0
        for session in sorted((s for s in rows if (s.provider or "") == name),
                              key=lambda s: s.id):
            seen.add(session.id)
            if cand.usable and (room is None or kept < room):
                kept += 1
                assigned[name] += 1
                out.append(Assignment(session.id, name, cand.model_id, name, keep=True,
                                      reason=(f"stays on {name} ({kept}/{cand.cap} of its cap)"
                                              if cand.cap is not None else f"stays on {name}")))
            else:
                movers.append((session, True))

    # Sessions whose current provider is not a candidate at all (undeclared, or
    # it does not serve this alias) also have to go somewhere.
    for session in sorted(rows, key=lambda s: s.id):
        if session.id not in seen:
            seen.add(session.id)
            movers.append((session, False))

    # Phase 2 — the movers, in id order, each to the least loaded usable
    # provider with room left.
    #
    # Only two kinds of session move: one whose provider is declared here but
    # cannot serve the alias (a config-level mismatch), and one that had nowhere
    # to be. A session sitting on a provider ds-router does not manage is left
    # ALONE: that provider is neither capped nor measured here, so it is neither
    # over cap nor exhausted, and a move would cost a cache reset for nothing.
    for session, over_cap in sorted(movers, key=lambda row: row[0].id):
        source = session.provider or ""
        if source and source not in declared:
            out.append(Assignment(session.id, source, "", source, keep=True,
                                  reason=f"{source} is not managed by ds-router; left alone"))
            continue
        dest = _destination(cands, assigned, skip_at=skip_at, weights=weights, now=now)
        if dest is None:
            out.append(Assignment(session.id, source, "", source, keep=True,
                                  unassigned=True,
                                  reason=f"no provider that serves {alias!r} can take it; left on "
                                         f"{source or 'nothing'}"))
            continue
        assigned[dest.name] += 1
        out.append(Assignment(session.id, dest.name, dest.model_id, source,
                              reason="moved off " + _why_leaving(source, cands, declared, alias, over_cap)))

    return sorted(out, key=lambda a: (a.session_id, a.provider))


def _as_session(row: Any) -> Session:
    """Coerce a `Session`, a dict, or a bare id string."""
    if isinstance(row, Session):
        return row
    if isinstance(row, dict):
        known = {k: v for k, v in row.items() if k in Session.__dataclass_fields__}
        known.setdefault("id", str(row.get("id") or row.get("session_id") or ""))
        if not known.get("id"):
            known["id"] = str(row.get("session_key") or "")
        return Session(**{k: ("" if v is None else v) for k, v in known.items()})
    return Session(str(row))


def counts(assignments: Iterable[Assignment]) -> dict[str, int]:
    """Target session count per provider. Handy for asserting a cap holds."""
    out: dict[str, int] = {}
    for a in assignments or []:
        if a.unassigned or not a.provider:
            continue
        out[a.provider] = out.get(a.provider, 0) + 1
    return out


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class TransportError(RuntimeError):
    """A transport could not carry a call: no backend, no socket, or a refusal."""


class Transport:
    """One method: send a JSON-RPC call, return its result dict."""

    def call(self, method: str, params: dict) -> dict:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - only LiveTransport holds a socket
        pass


class FakeTransport(Transport):
    """In-memory transport. Records every call and can be told to fail."""

    def __init__(self, response: Optional[dict] = None, fail: Iterable[str] = (),
                 fail_methods: Iterable[str] = ()) -> None:
        self.calls: list[dict] = []
        self.response = {"key": "model", "value": "ok", "scope": "session"} if response is None else dict(response)
        self.fail = {str(s) for s in fail}              # session_ids that raise
        self.fail_methods = {str(m) for m in fail_methods}

    def call(self, method: str, params: dict) -> dict:
        self.calls.append({"method": method, "params": dict(params or {})})
        if method in self.fail_methods:
            raise TransportError(f"fake failure for method {method}")
        if str((params or {}).get("session_id")) in self.fail:
            raise TransportError(f"fake failure for session {params.get('session_id')}")
        return dict(self.response)

    # --- assertions helpers (tests read exactly what was sent) ------------- #

    def methods(self) -> list[str]:
        return [c["method"] for c in self.calls]

    def session_ids(self) -> list[str]:
        return [str(c["params"].get("session_id")) for c in self.calls]

    def values(self) -> list[str]:
        return [str(c["params"].get("value") or c["params"].get("command") or "") for c in self.calls]


# --- LiveTransport: the desktop backend's /api/ws -------------------------- #

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _WSClient:
    """A minimal RFC6455 client — text frames, ping/pong, no dependencies.

    Deliberately hand-rolled: the repo takes pyyaml and nothing else, and this
    only ever needs to say one JSON line and read one reply.
    """

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._reader: Any = None

    def connect(self) -> None:
        parts = urllib.parse.urlsplit(self.url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        sock = socket.create_connection((host, port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        reader = sock.makefile("rb")
        status = reader.readline()
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        accept = ""
        while True:
            line = reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode("latin-1").partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accept = value.strip()
        if b" 101" not in status:
            sock.close()
            raise TransportError(f"websocket upgrade refused: {status.decode('latin-1').strip()[:80]}")
        if accept != expected:
            sock.close()
            raise TransportError("websocket handshake failed: bad Sec-WebSocket-Accept")
        self._sock, self._reader = sock, reader

    def _read_exactly(self, size: int) -> bytes:
        if size <= 0:
            return b""
        buf = self._reader.read(size)
        if not buf or len(buf) < size:
            raise TransportError("websocket connection closed")
        return buf

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def recv_text(self, timeout: Optional[float] = None) -> str:
        if timeout is not None and self._sock is not None:
            self._sock.settimeout(max(0.1, timeout))
        chunks: list[bytes] = []
        while True:
            b1, b2 = self._read_exactly(2)
            opcode = b1 & 0x0F
            sized = b2 & 0x7F
            masked = bool(b2 & 0x80)
            if sized == 126:
                sized = struct.unpack(">H", self._read_exactly(2))[0]
            elif sized == 127:
                sized = struct.unpack(">Q", self._read_exactly(8))[0]
            mask = self._read_exactly(4) if masked else b""
            payload = self._read_exactly(sized)
            if masked:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 0x9:      # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:      # pong
                continue
            if opcode == 0x8:
                raise TransportError("websocket closed by peer")
            chunks.append(payload)
            if b1 & 0x80:          # FIN
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            if self._sock is not None:
                self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = self._reader = None


@dataclass
class Backend:
    """A discovered Hermes backend process."""

    pid: int
    port: int
    token: str
    kind: str = "serve"

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/ws?token={urllib.parse.quote(self.token)}"

    def describe(self) -> str:
        return f"pid={self.pid} port={self.port} ({self.kind})"


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part for part in raw.decode("utf-8", "replace").split("\0") if part]


def _environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for entry in raw.decode("utf-8", "replace").split("\0"):
        if "=" in entry:
            key, _, value = entry.partition("=")
            out[key] = value
    return out


def _listening_ports(pid: int) -> list[int]:
    """TCP ports this pid is LISTENing on.

    Needed because the desktop spawns the backend with ``--port 0`` (an
    ephemeral port chosen by the kernel), so argv does not carry the real one.
    The port is recovered by matching this process's socket inodes against the
    namespace's listening sockets.
    """
    inodes: set[str] = set()
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        names = list(fd_dir.iterdir())
    except OSError:
        return []
    for entry in names:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    if not inodes:
        return []
    ports: set[int] = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = Path(f"/proc/{pid}/net/{table}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) < 10 or cols[3] != "0A":  # 0A = LISTEN
                continue
            if cols[9] not in inodes:
                continue
            try:
                ports.add(int(cols[1].rsplit(":", 1)[1], 16))
            except (ValueError, IndexError):
                continue
    return sorted(ports)


def discover_backends() -> list[Backend]:
    """Every Hermes backend process on this machine that carries a WS token.

    ``serve`` processes (the desktop's own backend) sort before ``dashboard``
    ones, because the dashboard may be bound to a different interface.
    """
    out: list[Backend] = []
    try:
        pids = sorted(int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit())
    except OSError:
        return []
    for pid in pids:
        argv = _cmdline(pid)
        if not any("hermes_cli.main" in arg for arg in argv):
            continue
        kind = "serve" if "serve" in argv else ("dashboard" if "dashboard" in argv else "")
        if not kind:
            continue
        env = _environ(pid)
        token = env.get("HERMES_DASHBOARD_SESSION_TOKEN", "")
        if not token:
            continue
        port = 0
        for index, arg in enumerate(argv):
            if arg == "--port" and index + 1 < len(argv):
                port = _as_int(argv[index + 1]) or 0
            elif arg.startswith("--port="):
                port = _as_int(arg.split("=", 1)[1]) or 0
        if port <= 0:
            ports = _listening_ports(pid)
            if not ports:
                continue
            port = ports[0]
        out.append(Backend(pid=pid, port=port, token=token, kind=kind))
    out.sort(key=lambda b: (0 if b.kind == "serve" else 1, b.pid))
    return out


class LiveTransport(Transport):
    """JSON-RPC over the desktop backend's ``/api/ws``.

    Constructed explicitly, never at import: it scans ``/proc`` and holds a
    socket. Raises `TransportError` with a usable message when there is no
    backend to talk to, so a caller can fall back to reading the state DB.
    """

    def __init__(self, *, host: str = "127.0.0.1", port: Optional[int] = None,
                 token: Optional[str] = None, pid: Optional[int] = None,
                 timeout: float = 10.0) -> None:
        self.timeout = float(timeout)
        self.backend = self._resolve(port=port, token=token, pid=pid)
        self.host = host
        self._ws: Optional[_WSClient] = None
        self._id = 0

    @staticmethod
    def _resolve(*, port: Optional[int], token: Optional[str],
                 pid: Optional[int]) -> Backend:
        if port and token:
            return Backend(pid=pid or 0, port=int(port), token=str(token), kind="explicit")
        found = discover_backends()
        if pid is not None:
            found = [b for b in found if b.pid == pid]
        if port:
            found = [b for b in found if b.port == int(port)]
        if not found:
            raise TransportError(
                "no Hermes backend found: looked for a `hermes_cli.main serve`/`dashboard` "
                "process with HERMES_DASHBOARD_SESSION_TOKEN in /proc/<pid>/environ. "
                "Pass --port and --token to point at one explicitly, or read the state DB "
                "with `--db`.")
        return found[0]

    # --- connection -------------------------------------------------------- #

    def _connect(self) -> _WSClient:
        if self._ws is not None:
            return self._ws
        url = self.backend.url
        if self.host and self.host != "127.0.0.1":
            url = url.replace("127.0.0.1", self.host, 1)
        client = _WSClient(url, timeout=self.timeout)
        try:
            client.connect()
        except TransportError:
            raise
        except OSError as exc:
            raise TransportError(f"could not reach {self.backend.describe()}: {exc}") from exc
        self._ws = client
        return client

    def call(self, method: str, params: dict) -> dict:
        """One JSON-RPC round trip. Reconnects once on a broken socket."""
        for attempt in (0, 1):
            try:
                return self._call_once(method, params)
            except TransportError:
                self.close()
                if attempt:
                    raise
        raise TransportError("unreachable")  # pragma: no cover

    def _call_once(self, method: str, params: dict) -> dict:
        client = self._connect()
        self._id += 1
        request = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        try:
            client.send_text(json.dumps(request))
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                frame = json.loads(client.recv_text(timeout=deadline - time.time()))
                if isinstance(frame, dict) and frame.get("id") == self._id:
                    if "error" in frame:
                        error = frame.get("error") or {}
                        raise TransportError(
                            f"{method} failed: {error.get('code')} {error.get('message')}")
                    result = frame.get("result")
                    return result if isinstance(result, dict) else {"result": result}
        except socket.timeout as exc:
            raise TransportError(f"{method}: no reply within {self.timeout:.0f}s") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise TransportError(f"{method}: transport error: {exc}") from exc
        raise TransportError(f"{method}: no reply within {self.timeout:.0f}s")

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None


# --------------------------------------------------------------------------- #
# Enumerating sessions
# --------------------------------------------------------------------------- #


def _session_columns(con: sqlite3.Connection) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute("pragma table_info(sessions)")}
    except sqlite3.Error:
        return set()


def _provider_of(billing_provider: Any, model_config: Any) -> str:
    """Hermes records the provider in two places; either is authoritative."""
    if billing_provider:
        return str(billing_provider)
    if model_config:
        try:
            parsed = json.loads(model_config) if isinstance(model_config, str) else model_config
        except (TypeError, ValueError):
            return ""
        if isinstance(parsed, dict) and parsed.get("provider"):
            return str(parsed["provider"])
    return ""


def _read_db(db_path: Optional[Path]) -> dict[str, str]:
    """``{session_id: provider}`` from the state DB, read-only. {} on any failure.

    A provider reading is an optimisation here: the RPC row does not carry it,
    so this fills the gap, and its absence must never break placement.
    """
    path = state_db_path(db_path)
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return {}
    try:
        cols = _session_columns(con)
        if "billing_provider" not in cols and "model_config" not in cols:
            return {}
        selected = [c for c in ("billing_provider", "model_config") if c in cols]
        for row in con.execute(f"select id, {', '.join(selected)} from sessions"):
            values = dict(zip(selected, row[1:]))
            provider = _provider_of(values.get("billing_provider"), values.get("model_config"))
            if provider:
                out[str(row[0])] = provider
    except sqlite3.Error:
        return out
    finally:
        con.close()
    return out


def sessions_from_db(db_path: Optional[Path] = None, *,
                     include_children: bool = False) -> list[Session]:
    """Fallback enumeration: live rows in the state DB, read-only.

    "Live" means ``ended_at is null``. Delegate children (subagents) and
    cron-sourced sessions are excluded by default: they inherit their parent's
    provider and are not the user's open windows, so moving them would be noise.
    """
    path = state_db_path(db_path)
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        cols = _session_columns(con)
        wanted = [c for c in ("id", "session_key", "billing_provider", "model_config",
                              "model", "title", "source", "ended_at", "archived", "hidden",
                              "last_activity_at") if c in cols]
        where = ["ended_at is null"] if "ended_at" in cols else []
        if "archived" in cols:
            where.append("coalesce(archived, 0) = 0")
        if "hidden" in cols:
            where.append("coalesce(hidden, 0) = 0")
        order = " order by last_activity_at desc" if "last_activity_at" in cols else ""
        sql = f"select {', '.join(wanted)} from sessions"
        if where:
            sql += " where " + " and ".join(where)
        sql += order
        rows = list(con.execute(sql))
    except sqlite3.Error:
        return []
    finally:
        con.close()

    out: list[Session] = []
    for row in rows:
        values = dict(zip(wanted, row))
        config = values.get("model_config")
        if not include_children:
            if str(values.get("source") or "") in ("subagent", "cron"):
                continue
            try:
                parsed = json.loads(config) if isinstance(config, str) else (config or {})
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, dict) and parsed.get("_delegate_from"):
                continue
        out.append(Session(id=str(values.get("id") or values.get("session_key") or ""),
                           provider=_provider_of(values.get("billing_provider"), config),
                           session_key=str(values.get("session_key") or ""),
                           model=str(values.get("model") or ""),
                           title=str(values.get("title") or "")))
    return out


def enumerate_sessions(transport: Optional[Transport] = None, *, db_path: Optional[Path] = None,
                       include_children: bool = False) -> list[Session]:
    """The sessions to place.

    Prefers ``session.active_list`` over *transport* (the in-process view, which
    is what the user actually has open). The RPC row does not carry the
    provider, so it is joined from the state DB by session id/key. With no
    transport, or on any RPC failure, it falls back to the state DB alone.
    """
    rows: list[dict] = []
    if transport is not None:
        try:
            result = transport.call("session.active_list", {})
            rows = list(result.get("sessions") or [])
        except Exception:
            rows = []
    if not rows:
        return sessions_from_db(db_path, include_children=include_children)

    providers = _read_db(db_path)
    out: list[Session] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("session_key") or "")
        sid = str(row.get("id") or key)
        provider = str(row.get("provider") or "") or providers.get(key, "") or providers.get(sid, "")
        out.append(Session(id=sid, provider=provider, session_key=key,
                           model=str(row.get("model") or ""), title=str(row.get("title") or "")))
    return out


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #


def params_for(assignment: Assignment, method: str = DEFAULT_METHOD) -> dict:
    """The RPC params for one move.

    ``config.set`` with ``"<model> --provider <p> --session"`` is what the
    desktop composer itself sends, and it returns ``scope: "session"`` — the
    override is per session and is never written to process-global env, which
    matters because the desktop hosts every same-profile session in one process.
    """
    if method == "slash.exec":
        return {"session_id": assignment.session_id,
                "command": f"/model {assignment.model_id} --provider {assignment.provider}"}
    return {"session_id": assignment.session_id, "key": "model",
            "value": f"{assignment.model_id} --provider {assignment.provider} --session"}


def apply(assignments: Iterable[Any], transport: Optional[Transport], *,
          dry_run: bool = True, method: str = DEFAULT_METHOD) -> list[Result]:
    """Send the per-session provider change for each moved session.

    Skipped, in this order: a session already on its target provider (nothing to
    send), and a session no provider could take. *dry_run* computes the exact
    params without sending them. One failed call does not abort the rest — a
    live fleet should not lose nine good moves because one session was busy.
    """
    rows = [a if isinstance(a, Assignment) else Assignment(**a) for a in (assignments or [])]
    if not dry_run and transport is None:
        raise TransportError("apply needs a transport; pass dry_run=True to only report")

    out: list[Result] = []
    for a in rows:
        if not a.changed:
            reason = ("already on target" if a.provider and a.provider == a.from_provider
                      else (a.reason or "no move planned"))
            out.append(Result(a.session_id, a.provider, a.from_provider, ok=True, skipped=True,
                              dry_run=dry_run, method=method, reason=reason))
            continue
        params = params_for(a, method)
        if dry_run:
            out.append(Result(a.session_id, a.provider, a.from_provider, ok=True, dry_run=True,
                              method=method, params=params, reason=a.reason))
            continue
        try:
            transport.call(method, params)
            out.append(Result(a.session_id, a.provider, a.from_provider, ok=True, method=method,
                              params=params, reason=a.reason))
        except Exception as exc:  # noqa: BLE001 - one bad session must not stop the fleet
            out.append(Result(a.session_id, a.provider, a.from_provider, ok=False, method=method,
                              params=params, error=f"{type(exc).__name__}: {exc}"[:200],
                              reason=a.reason))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def load_config(path: Optional[Path] = None) -> dict:
    import yaml

    return yaml.safe_load(Path(path or CONFIG).read_text()) or {}


def build_providers(config: dict) -> dict[str, dict]:
    """Flatten config.yaml's model table into ``{provider: {"models": {...}}}``."""
    models = config.get("models") or {}
    providers: dict[str, dict] = {}
    for name, spec in (config.get("providers") or {}).items():
        spec = dict(spec or {})
        spec["models"] = {alias: (table or {}).get(name) for alias, table in models.items()
                          if isinstance(table, dict)}
        providers[str(name)] = spec
    return providers


def hermes_home() -> Path:
    """Delegates to the shared resolver so the rule lives in one place."""
    from paths import hermes_home as _hermes_home
    return _hermes_home()


def collect_quotas(providers: dict, config: dict) -> dict:
    """Read live quotas, reusing router.py's collector — one config, one poller."""
    try:
        import router as router_mod  # lazy: the CLI lives in router.py too
    except Exception:
        return {}
    try:
        env = {**quota_mod.read_env(hermes_home() / ".env"), **os.environ}
        return router_mod.collect(providers, env, routing_cfg=config.get("routing") or {})
    except Exception:
        return {}


def _render_table(sessions: list[Session], assignments: list[Assignment], caps: dict,
                  alias: str, source: str) -> str:
    lines = [f"  alias       : {alias}",
             f"  sessions    : {len(sessions)} ({source})",
             f"  caps        : " + (", ".join(f"{k}={v}" for k, v in sorted(caps.items())) or "none declared")]
    target = counts(assignments)
    lines.append(f"  target      : " + (", ".join(f"{k}={v}" for k, v in sorted(target.items())) or "none"))
    kept = sum(1 for a in assignments if not a.changed)
    lines.append(f"  keep {kept}    move {len(assignments) - kept}")
    for name, cap in sorted((caps or {}).items()):
        used = target.get(name, 0)
        flag = "  OVER CAP" if isinstance(cap, int) and used > cap else ""
        lines.append(f"    {name:<14} {used}/{cap}{flag}")
    lines.append("")
    # Fixed-width columns with an explicit separator: provider names here are
    # 12 characters ("ollama-cloud"), so a width of 12 leaves no gap and the
    # model id runs into it.
    lines.append(f"  {'session':<14} {'from':<14} {'to':<14} {'model':<30} why")
    lines.append("  " + "-" * 108)
    for a in assignments:
        lines.append(f"  {a.session_id[:14]:<14} {(a.from_provider or '-'):<14} "
                     f"{(a.provider or '-'):<14} {(a.model_id or '-'):<30} {a.reason}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Spread live Hermes sessions across providers, respecting concurrency caps.")
    ap.add_argument("--plan", action="store_true", help="print the target assignment; write nothing (default)")
    ap.add_argument("--apply", action="store_true", help="apply it (required for any write)")
    ap.add_argument("--dry-run", action="store_true", help="print exactly what would be sent; write nothing")
    ap.add_argument("--alias", default=None, help="model alias to place (default: config default_model)")
    ap.add_argument("--db", action="store_true", help="enumerate sessions from state.db only (no backend RPC)")
    ap.add_argument("--db-path", default=None, help="state.db to read (default: ~/.hermes/state.db)")
    ap.add_argument("--port", type=int, default=None, help="backend port (default: discovered from /proc)")
    ap.add_argument("--token", default=None, help="backend WS token (default: /proc/<pid>/environ)")
    ap.add_argument("--children", action="store_true", help="include subagent/cron sessions")
    ap.add_argument("--limit", type=int, default=0, help="only print this many rows (0 = all)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.plan and args.apply:
        print("Choose one of --plan or --apply.", file=sys.stderr)
        return 2

    config = load_config()
    providers = build_providers(config)
    alias = args.alias or str(config.get("default_model") or "")
    routing_cfg = config.get("routing") or {}
    conc_cfg = routing_cfg.get("concurrency") or {}
    caps = dict(conc_cfg.get("caps") or {}) if conc_cfg.get("enabled", True) else {}

    transport: Optional[LiveTransport] = None
    source = "state.db"
    if not args.db:
        try:
            transport = LiveTransport(port=args.port, token=args.token)
            source = f"live backend {transport.backend.describe()}"
        except TransportError as exc:
            print(f"  note: {exc}\n  note: falling back to the state DB.", file=sys.stderr)

    sessions = enumerate_sessions(transport, db_path=Path(args.db_path) if args.db_path else None,
                                  include_children=args.children)
    quotas = collect_quotas(providers, config)
    assignments = plan(sessions, quotas, caps, providers, alias,
                       skip_at=float(routing_cfg.get("skip_at", SKIP_AT)),
                       weights=routing_cfg.get("window_weights") or WEIGHTS)

    if args.limit:
        assignments = assignments[: args.limit]

    if args.json:
        print(json.dumps({
            "alias": alias,
            "source": source,
            "caps": caps,
            "counts": counts(assignments),
            "assignments": [{"session_id": a.session_id, "from": a.from_provider,
                             "to": a.provider, "model_id": a.model_id, "keep": a.keep,
                             "unassigned": a.unassigned, "reason": a.reason} for a in assignments],
        }, indent=2))
    else:
        print()
        print(_render_table(sessions, assignments, caps, alias, source))
        print()

    if not args.apply and not args.dry_run:
        return 0

    if args.apply and transport is None:
        try:
            transport = LiveTransport(port=args.port, token=args.token)
        except TransportError as exc:
            print(f"--apply needs a backend: {exc}", file=sys.stderr)
            return 3

    dry_run = bool(args.dry_run) or not args.apply
    results = apply(assignments, transport, dry_run=dry_run)
    failed = [r for r in results if not r.ok]
    if not args.json:
        for r in results:
            if r.skipped:
                continue
            if r.dry_run:
                print(f"  would call {r.method} {json.dumps(r.params)}")
            elif r.ok:
                print(f"  moved {r.session_id} -> {r.provider}")
            else:
                print(f"  FAILED {r.session_id}: {r.error}")
        verb = "would move" if dry_run else "moved"
        print(f"\n  {verb} {sum(1 for r in results if not r.skipped)}"
              f"  skipped {sum(1 for r in results if r.skipped)}"
              f"  failed {len(failed)}")
        if dry_run:
            print("  (dry run — nothing was written)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
