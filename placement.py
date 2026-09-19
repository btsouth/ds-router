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
   and drops reasoning traces, so it must be justified. A session only moves when
   its provider is over its declared concurrency cap, exhausted, or cannot serve
   the requested alias. A provider whose *reading* failed is not a reason to move:
   the reading failed, not the endpoint, so a session already there stays and the
   provider is merely blocked as a destination until it reads again.
2. **A cap is a hard limit on the plan, not a scoring nudge.** `routing.py`
   prices a queue overage as soft pressure (queueing still completes). Here the
   plan must not create the queue in the first place.
3. **Never choose a provider that does not serve the model alias**, and never
   move a session onto a provider a reading says is spent.

Everything RPC-shaped goes through `Transport.call(method, params)`, so the
whole planner and applier are testable with `FakeTransport` and no backend.

A backend whose dashboard is published refuses Hermes' own session token by design,
so its sessions can only be reached the way a browser reaches them: a dashboard
credential, then a single-use WS ticket. `--gateway <origin>`, or a `gateway:` block
in config.yaml, reaches one that way. The credential comes from a 0600 file or an
env var, never from argv. Nothing changes without one: the loopback/token path is the
default. Shapes this transport cannot speak (https, a URL prefix, a wildcard address,
userinfo in the URL) are refused up front with the alternative named; the README's
"When it cannot find the backend" has the details.

Usage:

    python3 placement.py --plan            # print the target assignment; writes nothing
    python3 placement.py --plan --json
    python3 placement.py --apply           # apply it (explicit flag required)
    python3 placement.py --apply --dry-run # print exactly what would be sent
    python3 placement.py --plan --gateway http://10.0.0.5:9119 \
        --gateway-password-file ~/.hermes/dashboard-lan-password.txt
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import socket
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import load as load_mod  # noqa: E402
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
    # False when the provider could not be established for this session, which is
    # not the same as a session that genuinely has none. Only the enumeration can
    # tell those apart, so it is the enumeration that clears this.
    provider_known: bool = True


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
    """A cap only counts when it is a positive integer. Absent/0 = unbounded.

    ``None`` covers both "no cap was declared" and "a cap that cannot be read",
    which are not the same thing, so ``_cap_problems`` reports the second one
    before planning starts: a mistyped cap read as unlimited fills a provider
    past a limit it really has.
    """
    as_int = _as_int(value)
    return as_int if as_int is not None and as_int > 0 else None


class CapError(ValueError):
    """A declared concurrency cap that is not a positive integer."""


def _cap_problems(caps: Any) -> list[str]:
    """Declared caps that cannot be read. Absent and 0 stay meaning "unbounded"."""
    return load_mod.normalize_caps(caps)[1]


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
        """Safe to move a session ONTO. An unreadable reading is not trusted."""
        return not self.unreadable and not self.exhausted

    @property
    def may_keep(self) -> bool:
        """Safe to leave a session where it is.

        Deliberately weaker than ``usable``: an unreadable quota says the reading
        failed, not that the endpoint did, so a session already on that provider
        stays put. Ejecting it would pay a prompt-cache reset and lose the model's
        reasoning traces to avoid a provider whose inference path is probably
        fine. Hard exhaustion still qualifies, because that is real.
        """
        return not self.exhausted


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


def _draining(candidate: _Candidate, skip_at: float, now: float) -> bool:
    """True when *candidate* is heading for a throttle before it refills.

    A destination can be technically usable — neither hard-exhausted nor
    unreadable — and still be the wrong place to put a session that will live for
    hours. Two readings say so, both already used elsewhere in the router:

    * a window that has not reset yet is at or above ``skip_at``, the point the
      router stops *choosing* a provider even though it is not spent; this reads a
      window's own percentage, so it is stricter than the router's composite
      pressure line, which is deliberate: a placed session outlives one turn;
    * a window whose current burn rate reaches 100% before its own reset, which is
      the projection ``predicted_to_run_out`` is built on. Coming up short here is
      what a throttled conversation actually feels like.

    A window whose reset has already passed is ignored: upstream refilled it and
    the reading is stale, exactly as ``hard_exhausted`` treats it.
    """
    quota = candidate.quota
    if quota is None or quota.stale:
        return False
    for window in quota.windows:
        if window.resets_at is not None and window.resets_at <= now:
            continue
        if window.percent >= skip_at:
            return True
        pace = window.pace(now)
        if pace is not None and pace > 1.0:
            return True
    return False


def _destination(cands: dict[str, _Candidate], assigned: dict[str, int], *,
                 skip_at: float, weights: dict[str, float], now: float) -> Optional[_Candidate]:
    """The best place to put one more session, or None if nowhere can take it.

    Ranked by (load, risk, name). Load first is the whole point — spreading is
    what keeps every provider under its cap — with quota risk as a tie-break so
    two equally loaded providers resolve toward the healthier one. The name
    tiebreak is what makes the result deterministic.

    Providers that are draining (see ``_draining``) are held back as a second
    tier: they are used only when no destination with room is still healthy. Spare
    capacity on a provider that is about to throttle is not spare capacity, and a
    session sent there pays a prompt-cache reset when it is bounced again. Falling
    back keeps the fail-open promise: a fleet where every provider is draining
    still gets placed rather than stranded.
    """
    ranked: list[tuple[tuple, _Candidate]] = []
    for name in sorted(cands):
        cand = cands[name]
        if not cand.usable:
            continue
        load = cand.external + assigned.get(name, 0)
        if cand.cap is not None and load >= cand.cap:
            continue
        ranked.append(((load, _risk(cand, skip_at, weights, assigned.get(name, 0), now), name), cand))
    if not ranked:
        return None
    healthy = [row for row in ranked if not _draining(row[1], skip_at, now)]
    return min(healthy or ranked, key=lambda row: row[0])[1]


def _running_other_model(session: Session, alias: str, alias_ids: "set[str]") -> bool:
    """True when the session runs a known model other than the one being placed.

    The config.set wire format is ``<model-id> --provider <p> --session``, so a
    move sets the model too. Relocating a session that is deliberately running a
    different model would overwrite that choice, so it is left alone and only
    counted as load.

    The comparison must use every id the alias maps to, not the alias string: a
    session stores the PROVIDER-SPECIFIC id ('deepseek/deepseek-v4.1-flash' on
    CommandCode, 'cline-pass/deepseek-v4.1-flash' on ClinePass), so comparing to
    the bare alias 'deepseek-v4.1-flash' marks every session as different and
    freezes all movement.

    An unknown model ('' from a row that lacks it) is not treated as different, or
    nothing would ever be movable.
    """
    model = (session.model or "").strip()
    if not model:
        return False
    return model not in {str(v).strip() for v in (alias_ids or set())}


def _why_leaving(source: str, cands: dict[str, _Candidate], declared: dict[str, Any],
                 alias: str, over_cap: bool) -> str:
    """A one-line justification for moving a session, for the printed table.

    Each return value names the source provider, so a caller should present it as
    a clause after the move rather than concatenating it onto a verb phrase:
    "over its concurrency cap" reads from here, and the caller joins with a colon.
    """
    if not source:
        return "it had no provider yet"
    cand = cands.get(source)
    if cand is None:
        if source in declared:
            return f"{source} does not serve {alias!r}"
        return f"{source} is not a provider ds-router manages"
    if cand.exhausted:
        return f"{source} is exhausted"
    if over_cap:
        # Ahead of the unreadable case on purpose: an over-cap provider is the real
        # trigger here, and the README says an unreadable reading is never a reason
        # to move. Reporting the reading as the cause would contradict that rule and
        # hide the measurement that actually justified the move.
        return f"{source} is over its concurrency cap"
    if cand.unreadable:
        return f"{source} quota is unreadable"
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
    * a destination that is draining is used only when no destination that is not
      draining can take the session (see ``_destination``);
    * a declared cap that cannot be read as a positive integer raises, rather than
      being treated as "no cap";
    * the same input always produces the same output (every ordering in here is
      a sort on names and session ids, never on dict iteration order).

    *quotas_or_load* takes quotas, a load mapping, a `load.Load`, or a mix — see
    `normalize`. Absent readings are "unknown", not "unreadable": see
    `_Candidate.usable`.

    Returns one `Assignment` per session, in a deterministic order.
    """
    declared = dict(providers or {})
    # Normalised once here, so every consumer inside the plan (capacity, the
    # destination ranking, the load tie-break) reads the same number.
    readable_caps, cap_problems = load_mod.normalize_caps(caps)
    if cap_problems:
        raise CapError("unreadable concurrency cap(s) in config.yaml: "
                       + "; ".join(cap_problems)
                       + " (a cap is a positive integer; remove it or fix the value, "
                         "because a cap that cannot be read is not 'unlimited')")
    unknown_caps = [name for name in readable_caps if name not in declared]
    if unknown_caps:
        # A typo'd provider name means the limit is never enforced anywhere, which is
        # the same silent hole as a cap that cannot be read.
        print(f"  note: cap(s) declared for provider(s) that are not in config.yaml: "
              f"{', '.join(sorted(unknown_caps))} - they can never apply.", file=sys.stderr)
    caps = readable_caps
    weights = dict(weights or WEIGHTS)
    now = time.time() if now is None else now
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

    # Every id the alias maps to anywhere. A session stores its provider's own id,
    # so this set -- not the alias string -- is what tells "this session is already
    # on the model we are placing" from "this session is on something else".
    alias_ids = {cand.model_id for cand in cands.values()}

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
            if _running_other_model(session, alias, alias_ids):
                # Not ours to move: the wire format sets the model as well as the
                # provider, so relocating this session would silently replace the
                # model the user chose. It still occupies a slot.
                kept += 1
                assigned[name] += 1
                out.append(Assignment(session.id, name, session.model, name, keep=True,
                                      reason=f"stays on {name} (running {session.model}, not {alias})"))
                continue
            if cand.may_keep and (room is None or kept < room):
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
            if _running_other_model(session, alias, alias_ids):
                out.append(Assignment(session.id, session.provider or "", session.model,
                                      session.provider or "", keep=True,
                                      reason=f"not moved: running {session.model}, not {alias}"))
                continue
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
        if not session.provider_known:
            # Its provider could not be read, which is NOT the same as a session
            # that has none: acting on it would move a session that was probably
            # fine, on a claim we could not read.
            out.append(Assignment(session.id, source, "", source, keep=True,
                                  reason="its provider could not be read; left alone"))
            continue
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
                              reason=f"moved to {dest.name}: "
                                     + _why_leaving(source, cands, declared, alias, over_cap)))

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
    """A transport could not carry a call: no backend, no socket, or a refusal.

    ``status`` carries the HTTP status when there was one, so a caller can tell an
    authentication refusal (401/403) from another HTTP failure without matching on
    the message text. A message is for a human; a decision reads the code.
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def _http_status_code(line: bytes) -> Optional[int]:
    """The 3-digit code in an HTTP status line, or None when there is not one.

    ``b" 101" in status`` used to decide whether an upgrade was accepted, which is a
    substring test on a line a proxy is free to reword. Read the code instead.
    """
    for token in line.decode("latin-1", "replace").split()[:3]:
        if len(token) == 3 and token.isdigit():
            return int(token)
    return None


class NotSentError(TransportError):
    """The request never reached the backend, so repeating it cannot duplicate work.

    Only this fault is retried. A timeout, a closed socket or a malformed frame
    after the request went out may already have been applied by the backend, and
    config.set is a state mutation.
    """


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
        code = _http_status_code(status)
        if code != 101:
            sock.close()
            raise TransportError(
                f"websocket upgrade refused: {status.decode('latin-1').strip()[:80]}",
                status=code)
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
        # Best-effort teardown: the socket may already be gone, and raising from
        # close() would mask the original error that prompted the cleanup.
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


# --------------------------------------------------------------------------- #
# Gated backends: a dashboard credential, then a ticket
# --------------------------------------------------------------------------- #
#
# A backend bound to a non-loopback address never carries
# ``HERMES_DASHBOARD_SESSION_TOKEN``. Hermes engages a ticket-only auth gate for a
# non-loopback bind — and for a non-loopback ``dashboard.public_url`` even on a
# loopback bind — and that gate refuses the session token by design. So the
# sessions that matter most, the ones the Desktop app and a phone create against
# the shared backend, are exactly the ones a token-only transport cannot reach:
# it sees them in the database and can never steer them.
#
# A gated backend is reached the way a browser reaches it:
#
#     POST /auth/password-login    {"provider":"basic","username","password"} -> cookie
#     POST /api/auth/ws-ticket     (cookie) -> {"ticket", "ttl_seconds": 30}
#     GET  /api/ws?ticket=...      single-use upgrade
#
# Two properties of that gate shape the code below rather than taste:
#
#   * The ticket is single-use with a 30 s TTL, so one is minted per connection and
#     never cached: a reconnect that reused the previous ticket would be refused,
#     and a cached ticket would expire before the next call.
#   * Login is rate limited per client IP, so the cookie is held for the life of
#     the process and re-obtained only when the backend rejects it. Retrying a
#     wrong password would turn a clear refusal into a 429.


class GatewayAuthError(TransportError):
    """A gated backend refused the credential, so no ticket could be minted.

    Deliberately not "no backend found": the address is right and the fix is
    different (supply the right credential, not another port). Messages name the
    credential's *source* and never its value.
    """


@dataclass
class Gateway:
    """A gated backend reached with a dashboard credential.

    The password is excluded from ``repr`` so an accidental ``print``, a ``%r`` in a
    log line, or an assertion dump cannot become the leak this whole class exists to
    avoid. Every human-facing message goes through :meth:`describe`.
    """

    host: str
    port: int
    username: str
    password: str = field(repr=False)
    scheme: str = "http"
    source: str = "config"

    def __post_init__(self) -> None:
        # This transport speaks plaintext HTTP and WebSocket. A Gateway that claims
        # otherwise could only be built by hand, and the ticket_url mapping for it
        # would be a capability the transport does not have, so refuse at construction
        # rather than carry a dead branch.
        if self.scheme != "http":
            raise GatewayAuthError(
                f"a gated gateway is http only: this transport speaks plaintext HTTP "
                f"and WebSocket, so {self.scheme!r} cannot be reached")

    @property
    def authority(self) -> str:
        """``host:port``, with an IPv6 literal re-bracketed.

        Without the brackets the URL is unparseable, which used to be swallowed into
        a silent fall back to the state DB rather than reported.
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.authority}"

    def ticket_url(self, ticket: str) -> str:
        """The upgrade URL for *ticket*, quoted (a ticket is single-use and short-lived)."""
        return f"ws://{self.authority}/api/ws?ticket={urllib.parse.quote(ticket)}"

    def describe(self) -> str:
        return f"{self.authority} (gated, credential from {self.source})"


# The labels a credential file may carry. Matching a known set is what stops a prose
# line, or the file's own title, from being read as the password: a real file on this
# machine opens with a title and closes with paragraphs, and the username and password
# lines in it are space-separated rather than ``key: value``.
_CREDENTIAL_LABELS = ("origin", "alt", "username", "user", "password", "passwd")


def read_gateway_credential(path: Path) -> "Credential":
    """The credential in *path*, and any origins it names.

    The formats a real file arrives in, both in use on one machine here:

    * ``label: value`` (what Hermes' LAN dashboard file holds)
    * ``label value``, indented or not, with a title line and prose paragraphs
      around it (what a hand-written devbox file holds)

    Anything else that is a single line is taken as a bare secret, which keeps a
    hand-made ``chmod 600`` file working. Neither value is ever printed.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise GatewayAuthError(f"cannot read the gateway credential file {path}: {exc}") from exc

    fields: dict[str, str] = {}
    candidates: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidates.append(stripped)
        match = re.match(r"(?i)^([a-z]+)\s*[:=]?\s+(\S.*)$", stripped)
        if match and match.group(1).lower() in _CREDENTIAL_LABELS:
            # First win: a file that names the same label twice is ambiguous, and
            # guessing between them is how the wrong secret gets sent.
            fields.setdefault(match.group(1).lower(), match.group(2).strip())
    password = fields.get("password") or fields.get("passwd") or ""
    if not password and len(candidates) == 1:
        # One line, no label: a hand-made secret file.
        password = candidates[0]
    if not password:
        raise GatewayAuthError(
            f"the gateway credential file {path} holds no password line: expected "
            f"'password: <secret>' or 'password <secret>' (a single-line file holding "
            f"only the secret also works)")
    origins = tuple(value for value in (fields.get("origin"), fields.get("alt")) if value)
    return Credential(
        username=str(fields.get("username") or fields.get("user") or "").strip(),
        password=password, origins=origins, source=str(path))


@dataclass
class Credential:
    """One credential read from a file: the secret, and the origins the file names."""

    username: str
    password: str = field(repr=False)
    origins: tuple[str, ...] = ()
    source: str = ""


def _redact(url: str) -> str:
    """*url* with any password in it replaced, safe to put in a message.

    Every refusal message goes through this, not just the one that checks for a
    password: a URL that trips an earlier check would otherwise be echoed whole, and
    the check order is not something a caller can see.
    """
    parts = urllib.parse.urlsplit(str(url or ""))
    if not parts.password:
        return str(url or "")
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _checked_port(parts: object) -> int:
    """The port to talk to, refusing one that cannot be read.

    ``parts.port`` raises for a non-numeric value and returns None when absent, and
    ``or 80`` silently turned an explicit ``:0`` into 80. Both end up as a traceback
    or a wrong destination rather than a refusal.
    """
    raw = getattr(parts, "port", None)
    if raw is None:
        return 80
    if raw == 0:
        raise GatewayAuthError("port 0 is not a destination: give the port the backend serves on")
    return int(raw)


def resolve_gateway(url: str, *, username: Optional[str] = None,
                    password_file: Optional[str] = None,
                    password_env: Optional[str] = None,
                    warn: Optional[Callable[[str], None]] = None) -> Gateway:
    """Build a :class:`Gateway` from an origin plus a credential source.

    No ``--gateway-password`` flag exists on purpose: a password in argv is readable
    by every process on the machine. A credential file or an env var is the whole
    option set, and ``password_file`` and ``password_env`` are mutually exclusive
    rather than one silently winning.

    Shapes are refused rather than half-supported, because a URL that looks accepted
    and then fails later is worse than a clear no:

    * **userinfo in the URL.** It is ignored by design (a URL lands in shell history
      and in ``ps`` output), and silently ignoring it is how a user believes they
      configured a username they did not.
    * **https.** This transport speaks plaintext HTTP and WebSocket, so a TLS origin
      would send the ticket in the clear to a TLS port and fail meaninglessly. A TLS
      dashboard needs the second-backend recipe in the README.
    * **a URL prefix.** The paths are fixed (``/auth/password-login``,
      ``/api/auth/ws-ticket``, ``/api/ws``), so a dashboard served under a subpath
      would be signed in against the wrong URL.
    * **a wildcard address** (``0.0.0.0``, ``::``), which almost always means the
      user copied ``--host 0.0.0.0`` from their own ``serve`` command.

    A credential file that names other origins than the one configured is a warning
    rather than a refusal: one file legitimately covers a tailnet name and a LAN
    address for the same backend, and refusing would break that.
    """
    parts = urllib.parse.urlsplit(str(url or ""))
    safe = _redact(url)
    if parts.username or parts.password:
        raise GatewayAuthError(
            f"the gateway URL carries userinfo, which is ignored by design (a URL ends "
            f"up in shell history and in ps output): use --gateway-user and "
            f"--gateway-password-file, or the username and password_file keys in "
            f"config.yaml, instead of a URL like {safe}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise GatewayAuthError(
            f"a gated gateway needs an http origin such as http://192.168.1.88:9119, "
            f"got {safe!r}")
    if parts.scheme == "https":
        raise GatewayAuthError(
            f"{safe!r} is https, and this transport speaks plaintext HTTP and WebSocket "
            f"only: point it at a plain http origin on the tailnet or LAN, or use the "
            f"second-backend recipe in the README for a TLS dashboard")
    if parts.path.strip("/"):
        raise GatewayAuthError(
            f"{safe!r} carries a URL prefix, and a gated gateway must be an origin: the "
            f"sign-in and ticket paths are fixed, so a dashboard served under a subpath "
            f"is not supported")
    if parts.hostname in ("0.0.0.0", "::", "[::]"):
        raise GatewayAuthError(
            f"{safe!r} is a wildcard address, which is what a server binds to, not what a "
            f"client connects to: use the host you reach that backend on (its tailnet "
            f"name, or 127.0.0.1 if it is this machine)")
    if password_file and password_env:
        raise GatewayAuthError(
            "set one of password_file and password_env, not both: which one wins would "
            "otherwise be invisible")
    try:
        port = _checked_port(parts)
    except ValueError as exc:
        raise GatewayAuthError(
            f"the port in {safe!r} is not a number: {exc}") from exc

    file_origins: tuple[str, ...] = ()
    if password_file:
        path = Path(str(password_file)).expanduser()
        credential = read_gateway_credential(path)
        secret, file_user, file_origins = credential.password, credential.username, credential.origins
        source = str(path)
        _check_file_mode(path)
    elif password_env:
        secret = os.environ.get(str(password_env), "")
        source = f"${password_env}"
        file_user = ""
        if not secret:
            raise GatewayAuthError(f"{password_env} is unset or empty")
    else:
        raise GatewayAuthError(
            f"no credential for the gated gateway {safe}: set gateway.password_file in "
            f"config.yaml (see the commented example there), or pass "
            f"--gateway-password-file / --gateway-password-env")

    user = str(username or file_user or "").strip()
    if not user:
        raise GatewayAuthError(
            f"no dashboard username for the gated gateway {safe}: pass --gateway-user or "
            f"point at a credential file that carries a 'username [...]' line")
    gateway = Gateway(host=str(parts.hostname), port=port, username=user, password=secret,
                      scheme=str(parts.scheme), source=source)
    if warn is not None and file_origins:
        named = " or ".join(file_origins)
        if not any(_same_authority(entry, gateway.authority) for entry in file_origins):
            warn(f"the credential file {source} names {named}, and you pointed at "
                 f"{gateway.authority}: the password will be sent to {gateway.authority}")
    return gateway


def _same_authority(url: str, authority: str) -> bool:
    """True when *url* names the same host:port as *authority* (port defaulted by scheme)."""
    parts = urllib.parse.urlsplit(str(url or ""))
    if not parts.hostname:
        return False
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    return f"{parts.hostname}:{port}" == authority


def _check_file_mode(path: Path) -> None:
    """Refuse a credential file that other users can read.

    The README tells the reader to keep this file at 0600, so reading a 0644 one
    without a word would be the doc lying.
    """
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return  # the read itself already reported a missing file
    if mode & 0o077:
        raise GatewayAuthError(
            f"the gateway credential file {path} is mode {mode:03o}, which other users can "
            f"read: run chmod 600 {path}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface a redirect as its status instead of following it.

    Following one cannot authenticate anything here, and a login that answered with
    a redirect means the credential was not accepted. Hiding that behind a followed
    3xx is how "wrong password" turns into "no ticket".
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class GatewaySession:
    """One signed-in session against a gated backend: a cookie, then tickets.

    Three properties of the gate shape this rather than taste:

    * Login is rate limited per client IP, so the cookie is held for the life of the
      process, and a credential that has already been refused is **latched**: the
      refusal is re-raised without another request, because the same password cannot
      start working mid-run and a retry loop turns a clear refusal into a 429.
    * The ticket is single-use with a 30 s TTL, so one is minted per connection and
      never cached.
    * The requests are made by a purpose-built opener with proxies switched off. A
      login is one POST to one host with a password in the body, and an environment
      proxy would receive that password and the session cookie in full. The WebSocket
      half is a raw socket and never used a proxy anyway, so honouring one would make
      the transport incoherent as well as leaky.
    """

    def __init__(self, gateway: Gateway, *, timeout: float = 10.0) -> None:
        self.gateway = gateway
        self.timeout = float(timeout)
        self.logins = 0  # attempts; observable so a retry loop cannot hide here
        self._cookie_held = False
        self._refusal: Optional[GatewayAuthError] = None
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),        # never route a password via a proxy
            urllib.request.HTTPCookieProcessor(self._jar), _NoRedirect())

    def forget(self) -> None:
        """Drop the cookie so the next mint signs in again. A latched refusal stays."""
        self._jar.clear()
        self._cookie_held = False

    def _post(self, path: str, payload: Optional[dict] = None) -> tuple[int, Any]:
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.gateway.origin + path, data=body, method="POST")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(request, timeout=self.timeout) as reply:
                return int(reply.status), _json_or_empty(reply.read(65536))
        except urllib.error.HTTPError as exc:
            return int(exc.code), _json_or_empty(exc.read(65536))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise GatewayAuthError(
                f"cannot reach the gated gateway at {self.gateway.origin}: {exc}") from exc

    def _detail(self, body: Any) -> str:
        """The server's own explanation, with the password scrubbed out of it.

        A message must never carry the secret, and this is the one string here that a
        remote server writes. A backend that echoes what it was sent is exactly how a
        password reaches a terminal, a journal or a CI log.
        """
        text = ""
        if isinstance(body, dict):
            text = str(body.get("detail") or body.get("error") or "")
        if not text:
            return ""
        return text.replace(self.gateway.password, "***")[:200]

    def login(self) -> None:
        if self._refusal is not None:
            # Already refused once in this process. The credential cannot change while
            # we run, and retrying is what would burn the per-IP login budget.
            raise self._refusal
        status, body = self._post("/auth/password-login", {
            "provider": "basic", "username": self.gateway.username,
            "password": self.gateway.password})
        self.logins += 1
        if status == 200:
            if not list(self._jar):
                raised = GatewayAuthError(
                    f"sign-in to the gated gateway {self.gateway.origin} answered 200 but "
                    f"set no session cookie, so no ticket can be minted")
                self._refusal = raised
                raise raised
            self._cookie_held = True
            return
        detail = self._detail(body)
        causes = {
            401: f"the credential from {self.gateway.source} was refused",
            404: "this backend advertises no password provider (the 'basic' provider "
                 "is not registered); see /api/auth/providers",
            429: "the backend rate limited the login attempts; wait a minute",
            503: "the backend could not read its credential store",
        }
        raised = GatewayAuthError(
            f"sign-in to the gated gateway {self.gateway.origin} failed: "
            f"{causes.get(status, f'HTTP {status}')}{f' ({detail})' if detail else ''}")
        self._refusal = raised
        raise raised

    def mint_ticket(self) -> str:
        """A fresh single-use ticket; re-signs in at most once when the cookie is stale."""
        if not self._cookie_held:
            # No cookie yet, so a ticket request now is a guaranteed 401 that the
            # backend records as a rejected upgrade. Sign in first.
            self.login()
        status, body = self._post("/api/auth/ws-ticket")
        if status == 401:
            # The real "this cookie is stale" signal: the ticket endpoint is the only
            # one that answers 401 for it.
            self.forget()
            self.login()
            status, body = self._post("/api/auth/ws-ticket")
        if status != 200:
            detail = self._detail(body)
            hint = ""
            if status in (401, 404):
                hint = ("; if this backend binds loopback and its dashboard.public_url is "
                        "loopback too, it is not gated at all: drop --gateway and the "
                        "token path needs no credential")
            raise GatewayAuthError(
                f"the gated gateway {self.gateway.origin} refused a WS ticket: "
                f"HTTP {status}{f' ({detail})' if detail else ''}{hint}")
        ticket = body.get("ticket") if isinstance(body, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise GatewayAuthError(
                f"the gated gateway {self.gateway.origin} returned no ticket")
        return ticket


def _json_or_empty(raw: Any) -> Any:
    try:
        return json.loads(raw or b"")
    except (ValueError, TypeError):
        return {}


def gateway_from_config(config: dict, args: Any = None,
                        warn: Optional[Callable[[str], None]] = None) -> Optional[Gateway]:
    """The gated gateway to steer: explicit flags win, else the config ``gateway:`` block.

    Absent everywhere means ``None``, which keeps every existing invocation on the
    loopback/token path it has today.
    """
    block = config.get("gateway") or {}
    if not isinstance(block, dict):
        raise GatewayAuthError("the 'gateway' block in config.yaml must be a mapping")
    url = (getattr(args, "gateway", None) or block.get("url") or "")
    if not url:
        return None
    return resolve_gateway(
        url,
        username=getattr(args, "gateway_user", None) or block.get("username"),
        password_file=getattr(args, "gateway_password_file", None) or block.get("password_file"),
        password_env=getattr(args, "gateway_password_env", None) or block.get("password_env"),
        warn=warn)


@dataclass
class Backend:
    """A discovered Hermes backend process."""

    pid: int
    port: int
    token: str
    kind: str = "serve"
    gateway: Optional[Gateway] = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/ws?token={urllib.parse.quote(self.token)}"

    def describe(self) -> str:
        if self.gateway is not None:
            return self.gateway.describe()
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
                 gateway: Optional[Gateway] = None,
                 timeout: float = 10.0) -> None:
        self.timeout = float(timeout)
        self.gateway = gateway
        self.backend = self._resolve(port=port, token=token, pid=pid, gateway=gateway)
        self.host = host
        self._ws: Optional[_WSClient] = None
        self._id = 0
        self._session: Optional[GatewaySession] = None

    @staticmethod
    def _resolve(*, port: Optional[int], token: Optional[str],
                 pid: Optional[int], gateway: Optional[Gateway] = None) -> Backend:
        if gateway is not None:
            # A gated backend is addressed explicitly: it has no token to discover
            # and may not even be on this machine.
            return Backend(pid=0, port=int(gateway.port), token="", kind="gated",
                           gateway=gateway)
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
                "A backend bound to a non-loopback address never carries that token: Hermes "
                "engages a ticket-only auth gate for any non-loopback bind (and for a "
                "non-loopback dashboard.public_url even on a loopback bind), and that gate "
                "refuses the session token by design. Point this at a loopback-bound backend "
                "instead: `serve --host 127.0.0.1` with a stable HERMES_DASHBOARD_SESSION_TOKEN "
                "and HERMES_DASHBOARD_PUBLIC_URL set to a loopback URL. "
                "Pass --port and --token to point at one explicitly, or read the state DB "
                "with `--db`.")
        return found[0]

    # --- connection -------------------------------------------------------- #

    def _gateway_session(self) -> GatewaySession:
        gateway = self.backend.gateway
        if gateway is None:
            raise TransportError("this transport has no gated gateway configured")
        if self._session is None:
            self._session = GatewaySession(gateway, timeout=self.timeout)
        return self._session

    def _ws_url(self) -> str:
        """Where to connect: a token URL for a loopback backend, a fresh ticket for a gated one.

        Minted here, per connection, because the ticket is single-use with a 30 s TTL:
        caching one would hand the retry path a credential the backend has already
        burned, and the next call a credential that expired.
        """
        if self.backend.gateway is not None:
            return self.backend.gateway.ticket_url(self._gateway_session().mint_ticket())
        url = self.backend.url
        if self.host and self.host != "127.0.0.1":
            url = url.replace("127.0.0.1", self.host, 1)
        return url

    def _connect(self) -> _WSClient:
        if self._ws is not None:
            return self._ws
        url = self._ws_url()
        client = _WSClient(url, timeout=self.timeout)
        try:
            client.connect()
        except NotSentError:
            raise
        except TransportError as exc:
            # An upgrade can be refused for reasons that are not about the credential:
            # Hermes closes a WS before accept with a code the client sees only as
            # HTTP 403 (bad credential, host mismatch, chat disabled all look alike),
            # and the real "this cookie is stale" signal is the 401 from the ticket
            # request, which `mint_ticket` already handles. So a refusal here must not
            # cost a sign-in: logins are rate limited per client IP.
            #
            # A connection is never a mutation: safe to repeat.
            raise NotSentError(str(exc), status=exc.status) from exc
        except OSError as exc:
            raise NotSentError(f"could not reach {self.backend.describe()}: {exc}") from exc
        self._ws = client
        return client

    def call(self, method: str, params: dict) -> dict:
        """One JSON-RPC round trip. Reconnects once on a broken connection.

        Only a fault that happened BEFORE the request went out is retried. A
        well-formed error reply is a final answer: retrying it would re-send a
        state mutation like config.set that the backend already processed, and
        would hide the real error behind an eventual socket error. A timeout or a
        closed socket after the send is in the same class: the backend may have
        applied the change before the reply was lost, so repeating it can apply it
        twice while reporting a failure.
        """
        for attempt in (0, 1):
            try:
                return self._call_once(method, params)
            except BackendError:
                raise  # the server answered; do not repeat the request
            except NotSentError:
                self.close()
                if attempt:
                    raise
            except TransportError:
                self.close()
                raise
        raise TransportError("unreachable")  # pragma: no cover

    def _call_once(self, method: str, params: dict) -> dict:
        client = self._connect()
        self._id += 1
        request = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        try:
            client.send_text(json.dumps(request))
        except (OSError, socket.timeout) as exc:
            raise NotSentError(f"{method}: could not send: {exc}") from exc
        try:
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                frame = json.loads(client.recv_text(timeout=deadline - time.time()))
                if isinstance(frame, dict) and frame.get("id") == self._id:
                    if "error" in frame:
                        error = frame.get("error") or {}
                        raise BackendError(
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


# Hermes writes this into billing_provider for EVERY named custom provider: it is
# the runtime identity, not the name the user configured. Treating it as a
# provider name makes those sessions look unmanaged, so their real provider's load
# is undercounted and they are never spread.
_GENERIC_PROVIDER = "custom"


def _config_provider(model_config: Any) -> str:
    """The provider named inside a session's stored model config, or ''."""
    if not model_config:
        return ""
    try:
        parsed = json.loads(model_config) if isinstance(model_config, str) else model_config
    except (TypeError, ValueError):
        return ""
    if isinstance(parsed, dict) and parsed.get("provider"):
        return str(parsed["provider"])
    return ""


def _provider_of(billing_provider: Any, model_config: Any) -> str:
    """The provider a session is configured to use, or '' when unknowable.

    ``model_config.provider`` is preferred because it holds the durable config key
    (``clinepass``), while ``billing_provider`` holds the runtime identity, which
    is the generic ``custom`` for any named custom provider. Falling back to
    ``billing_provider`` keeps the pre-existing behaviour for the built-in
    providers, whose model config is empty.

    The rule itself lives in ``load.provider_of``, so the planner and the load
    reading cannot disagree about which provider the same session is on.
    """
    return load_mod.provider_of(billing_provider, model_config)


class BackendError(RuntimeError):
    """The backend answered with a JSON-RPC error. Final: never retried."""


class ProviderJoinError(RuntimeError):
    """Live sessions were listed, but their providers could not be read at all."""


class StoreUnreadable(RuntimeError):
    """The session store exists but could not be read, which is not an empty fleet."""


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
        print(f"  note: no session store at {path}; nothing to enumerate from it.",
              file=sys.stderr)
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        raise StoreUnreadable(f"cannot open {path}: {exc}") from exc
    try:
        cols = _session_columns(con)
        wanted = [c for c in ("id", "session_key", "billing_provider", "model_config",
                              "model", "title", "source", "ended_at", "archived", "hidden",
                              "last_activity_at") if c in cols]
        if not wanted:
            # A sqlite file with no sessions columns is not a Hermes store. Building
            # "select  from sessions" out of an empty column list produced a SQL syntax
            # error, which reads as a bug here rather than as "wrong file".
            raise StoreUnreadable(f"{path} has no sessions table with the columns a Hermes "
                                  "session store has; is this the right file?")
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
    except sqlite3.Error as exc:
        raise StoreUnreadable(f"cannot read sessions from {path}: {exc}") from exc
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

    Raises ``ProviderJoinError`` when the sessions are known but not ONE of their
    providers could be read. Without that, an unreadable store makes every session
    look provider-less, and since "no provider yet" is a reason to move, a
    one-session plan becomes a whole-fleet rewrite.
    """
    rows: list[dict] = []
    listed = False
    if transport is not None:
        try:
            result = transport.call("session.active_list", {})
            rows = list(result.get("sessions") or [])
            listed = True
        except GatewayAuthError:
            # A credential failure is not a telemetry hiccup, and it must not become a
            # state-DB plan: the header would still say "live backend" while steering
            # had quietly stopped working, and --apply would then send stored ids the
            # backend refuses. It reaches the caller as itself.
            raise
        except Exception as exc:
            # A failed list silently changes WHICH sessions are acted on, so say so.
            print(f"  note: session.active_list failed ({exc}); "
                  f"falling back to the state store.", file=sys.stderr)
            rows = []
    if listed and not rows:
        return []  # a successful empty listing means nothing is open
    if not rows:
        # The DB path yields ``sessions.id``, which is not the same namespace as
        # the runtime id the RPC view returns. config.set resolves the runtime id,
        # so a DB-only run may send identifiers the backend cannot match. Read-only
        # planning is unaffected; a write from here is best-effort.
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
    if out and not providers and not any(s.provider for s in out):
        raise ProviderJoinError(
            f"{len(out)} live session(s) found but no provider could be read for any of them "
            f"(state store: {state_db_path(db_path)}). Refusing to act, because every session "
            f"would look provider-less and be moved.")
    # A PARTIAL join is the same hazard with a narrower trigger. If some sessions
    # resolved and others did not, the unresolved ones look like sessions that
    # never had a provider, which the planner treats as a reason to move them: that
    # is a prompt-cache reset for a session that was probably fine, justified by a
    # claim we could not read. Mark them, so the planner leaves them where they are
    # and says why.
    unjoined = [s for s in out if not s.provider]
    if unjoined and len(unjoined) < len(out):
        print(f"  note: {len(unjoined)} of {len(out)} live session(s) have no readable "
              "provider; they are left where they are rather than moved as if they had none.",
              file=sys.stderr)
        for session in unjoined:
            session.provider_known = False
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


def _read_reply(reply: Any) -> tuple[str, str]:
    """Interpret a config.set reply: ("ok", "") or ("blocked"/"error", detail).

    A returned call is not a completed move. Hermes answers config.set with
    envelopes that mean the opposite of success:

    * ``confirm_required`` — the backend deliberately applied NOTHING. Its
      large-context guard asks for confirmation when the destination uses a
      different model id than the session holds, which is exactly a
      cross-provider move, so the biggest sessions silently do not move.
    * ``deferred`` — the pick was stashed for the next turn, not applied now.
    * an ``error`` member — a failure delivered inside a well-formed reply.

    Anything else (including a plain string) is reported as ok, so a transport that
    returns a bare success token is not mistaken for a failure. A reply that carries
    NO result is different: it is reported as ``unconfirmed``, because the backend
    never said the move happened and this whole function exists to keep a returned
    call from being read as a completed action.
    """
    if isinstance(reply, dict):
        if reply.get("confirm_required"):
            return "blocked", str(reply.get("confirm_message")
                                  or "the backend asked for confirmation and applied nothing")
        if reply.get("deferred"):
            return "deferred", "the backend stashed this change for the next turn instead of applying it"
        error = reply.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or error.get("data") or "unspecified error"
            return "error", f"backend error {code}: {message}" if code else str(message)
        if error:
            return "error", str(error)
        # The transport wraps a non-dict result as {"result": <value>}, so a null
        # result arrives as exactly this shape.
        if set(reply) <= {"result"} and reply.get("result") in (None, "", {}, []):
            return "unconfirmed", ("the backend returned no result for this call, so the "
                                  "move cannot be confirmed")
        return "ok", ""
    if reply in (None, "", {}, []):
        return "unconfirmed", ("the backend returned no result for this call, so the move "
                              "cannot be confirmed")
    return "ok", ""


class StoredIdError(RuntimeError):
    """A write was attempted with state.db ids, which the backend will reject."""


def apply(assignments: Iterable[Any], transport: Optional[Transport], *,
          dry_run: bool = True, method: str = DEFAULT_METHOD,
          session_ids_are_stored: bool = False) -> list[Result]:
    """Send the per-session provider change for each moved session.

    Skipped, in this order: a session already on its target provider (nothing to
    send), and a session no provider could take. *dry_run* computes the exact
    params without sending them. One failed call does not abort the rest — a
    live fleet should not lose nine good moves because one session was busy.

    *session_ids_are_stored* must be set when the assignments came from the
    state.db fallback, whose ids the backend cannot resolve (verified live: it
    answers 4001 "config.set model requires a live session"). Refusing up front
    beats emitting one identical failure per session.
    """
    rows = [a if isinstance(a, Assignment) else Assignment(**a) for a in (assignments or [])]
    if not dry_run and transport is None:
        raise TransportError("apply needs a transport; pass dry_run=True to only report")
    if not dry_run and session_ids_are_stored:
        raise StoredIdError(
            "cannot write with state.db session ids: the backend resolves live session "
            "ids and answers 4001 for stored ones. Drop --db so sessions come from the "
            "running backend, or use --plan to inspect the DB view without writing.")

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
            reply = transport.call(method, params)
            verdict, detail = _read_reply(reply)
            if verdict == "blocked":
                # The backend applied nothing and is waiting for confirmation.
                # This run is non-interactive and the intent is unambiguous, so
                # confirm once rather than reporting a move that did not happen.
                retry = dict(params, confirm_expensive_model=True)
                reply = transport.call(method, retry)
                verdict, detail = _read_reply(reply)
                params = retry
            if verdict == "ok":
                out.append(Result(a.session_id, a.provider, a.from_provider, ok=True, method=method,
                                  params=params, reason=a.reason))
            else:
                out.append(Result(a.session_id, a.provider, a.from_provider, ok=False, method=method,
                                  params=params, reason=a.reason, error=detail))
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
    ap.add_argument("--gateway", default=None, metavar="URL",
                    help="steer sessions on a gated (non-loopback) backend, e.g. "
                         "http://192.168.1.88:9119 (default: config gateway.url). Needs a "
                         "dashboard credential; without it those sessions are only visible")
    ap.add_argument("--gateway-user", default=None,
                    help="dashboard username (default: config, else the credential file's)")
    ap.add_argument("--gateway-password-file", default=None, metavar="PATH",
                    help="file holding the dashboard password "
                         "(default: config gateway.password_file)")
    ap.add_argument("--gateway-password-env", default=None, metavar="VAR",
                    help="env var holding the dashboard password "
                         "(default: config gateway.password_env)")
    ap.add_argument("--children", action="store_true", help="include subagent/cron sessions")
    ap.add_argument("--limit", type=int, default=0, help="only print this many rows (0 = all)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.plan and args.apply:
        print("Choose one of --plan or --apply.", file=sys.stderr)
        return 2

    config = load_config()
    gateway: Optional[Gateway] = None
    if not args.db:
        # `--db` means "plan from the state store, do not touch a live backend", so the
        # gateway is not resolved at all: a broken gateway config must not refuse a
        # read-only run that never wanted one.
        try:
            gateway = gateway_from_config(
                config, args, warn=lambda note: print(f"  note: {note}", file=sys.stderr))
        except GatewayAuthError as exc:
            print(f"refusing to start: {exc}", file=sys.stderr)
            return 2
    if gateway is not None and (args.port or args.token):
        # A gated backend is addressed by the gateway, so these are inert rather than
        # overridden: say so, because silently ignoring a flag looks like it worked.
        print("  note: --port/--token are ignored while a gateway is configured.",
              file=sys.stderr)
    providers = build_providers(config)
    alias = args.alias or str(config.get("default_model") or "")
    routing_cfg = config.get("routing") or {}
    conc_cfg = routing_cfg.get("concurrency") or {}
    caps = dict(conc_cfg.get("caps") or {}) if conc_cfg.get("enabled", True) else {}

    transport: Optional[LiveTransport] = None
    source = "state.db"
    if not args.db:
        try:
            transport = LiveTransport(port=args.port, token=args.token, gateway=gateway)
            source = f"live backend {transport.backend.describe()}"
        except TransportError as exc:
            print(f"  note: {exc}\n  note: falling back to the state DB.", file=sys.stderr)

    from_db = bool(args.db) or transport is None
    try:
        sessions = enumerate_sessions(transport, db_path=Path(args.db_path) if args.db_path else None,
                                      include_children=args.children)
    except ProviderJoinError as exc:
        print(f"refusing to plan: {exc}", file=sys.stderr)
        print("  Reading the provider for every live session failed, so each one would "
              "look provider-less and be moved.", file=sys.stderr)
        print("  Check that state.db is readable, or pass --db-path.", file=sys.stderr)
        return 5
    except StoreUnreadable as exc:
        print(f"refusing to plan: {exc}", file=sys.stderr)
        print("  An unreadable session store is not an empty fleet: reporting no sessions "
              "would look like a clean run.", file=sys.stderr)
        return 5
    except GatewayAuthError as exc:
        # The one failure that must never degrade into a plan: the sessions would come
        # from state.db, the header would still name a live backend, and steering would
        # have stopped without the exit code saying so.
        print(f"refusing to plan: {exc}", file=sys.stderr)
        print("  The gated backend could not be signed in to, so no session could be "
              "steered. Fix the credential (or drop --gateway to plan from state.db on "
              "purpose).", file=sys.stderr)
        return 2
    quotas = collect_quotas(providers, config)
    # The concurrency reading has to be handed to the planner, or the rule that a
    # provider already busy from work outside this plan takes no new session holds
    # only for callers of the library and not for the shipped CLI.
    session_db = Path(args.db_path) if args.db_path else None
    try:
        live_load = load_mod.active_by_provider(session_db=session_db)
    except Exception as exc:  # a load reading is an optimisation and must not stop a plan
        live_load = load_mod.Load({}, "unreadable", f"{type(exc).__name__}: {exc}")
    readings: dict[str, dict[str, Any]] = {name: {"quota": q} for name, q in quotas.items()}
    for name, count in live_load.counts.items():
        readings.setdefault(name, {})["load"] = count

    try:
        assignments = plan(sessions, readings, caps, providers, alias,
                           skip_at=float(routing_cfg.get("skip_at", SKIP_AT)),
                           weights=routing_cfg.get("window_weights") or WEIGHTS)
    except CapError as exc:
        print(f"refusing to plan: {exc}", file=sys.stderr)
        return 7

    if not quotas and providers:
        print("  note: no quota readings were collected, so this plan spreads by load "
              "only and knows nothing about headroom.", file=sys.stderr)
        print("  note: check the provider keys and the quota endpoints "
              "(router.py --dry-run shows what each provider reports).", file=sys.stderr)
    if live_load.error:
        # Surface the reading's own note whenever there is one, readable or not: a
        # partial count is smaller than reality, and a silent undercount looks
        # exactly like a healthy fleet.
        print(f"  note: load reading: {live_load.error}", file=sys.stderr)
    if not live_load.readable:
        print(f"  note: the concurrency reading is unusable ({live_load.source}), "
              "so no provider is charged for work already running on it.", file=sys.stderr)
        print("  note: caps still hold for the sessions in this plan.", file=sys.stderr)
    elif live_load.counts:
        print(f"  note: {live_load.total} session(s) active elsewhere count against their "
              f"provider's cap ({live_load.source}).", file=sys.stderr)

    # --limit shortens the PRINTED rows only. Truncating the assignment list
    # itself would make `--apply --limit N` write an arbitrary prefix of the plan,
    # chosen by session-id sort order rather than by importance.
    shown = assignments[: args.limit] if args.limit else assignments

    if args.json:
        print(json.dumps({
            "alias": alias,
            "source": source,
            "caps": caps,
            "counts": counts(assignments),
            "load": {"source": live_load.source, "readable": live_load.readable,
                     "error": live_load.error,
                     "counts": dict(sorted(live_load.counts.items()))},
            "printed": len(shown),
            "assignments": [{"session_id": a.session_id, "from": a.from_provider,
                             "to": a.provider, "model_id": a.model_id, "keep": a.keep,
                             "unassigned": a.unassigned, "reason": a.reason} for a in assignments],
        }, indent=2))
    else:
        print()
        print(_render_table(sessions, shown, caps, alias, source))
        if len(shown) < len(assignments):
            print(f"  (showing {len(shown)} of {len(assignments)} rows; "
                  f"the plan itself is not truncated)")
        print()

    if not args.apply and not args.dry_run:
        return 0

    if args.apply and transport is None:
        try:
            transport = LiveTransport(port=args.port, token=args.token, gateway=gateway)
        except TransportError as exc:
            print(f"--apply needs a backend: {exc}", file=sys.stderr)
            return 3

    dry_run = bool(args.dry_run) or not args.apply
    try:
        results = apply(assignments, transport, dry_run=dry_run,
                        session_ids_are_stored=from_db)
    except StoredIdError as exc:
        print(f"refusing to apply: {exc}", file=sys.stderr)
        return 6
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
