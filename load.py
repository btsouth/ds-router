"""Live session load per provider, read from Hermes' own session store.

Why this exists: a provider's concurrency cap applies to requests in flight,
not to sessions that merely exist. Ollama Cloud Pro allows 3 concurrent
requests and queues beyond that (rejecting when the queue is full), so the
number of *active* sessions on a provider is a real routing input that quota
percentages cannot express.

Two signals, deliberately:

* ``session_turn_leases`` — Hermes' own record of which sessions hold a turn.
  This is the closest thing to "in flight" available from outside the process.
  Leases renew while a turn runs, so a not-yet-expired lease with recent
  acquisition means active work.
* ``sessions.last_activity_at`` — a fallback when the lease table is empty
  (older Hermes, or a surface that does not lease).

Both are read-only. Nothing here writes to the session store.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class Load:
    """Active session counts per provider, plus how the figure was obtained."""

    counts: dict[str, int] = field(default_factory=dict)
    source: str = "none"
    error: str = ""

    def count(self, provider: str) -> int:
        return int(self.counts.get(provider, 0))

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def readable(self) -> bool:
        """True when an empty count means "idle" rather than "unknown".

        These used to be the same value: a store whose tables could not be read
        returned counts {} and source "none", exactly like a healthy store with
        nothing running, so a saturated provider looked idle and its cap stopped
        constraining anything. Callers cannot fix that without being able to tell
        the two apart, which is what this field is for.

        "disabled" counts as readable: the caller switched the reading off in
        config, which is a choice rather than a failure. "leases-unknown" does NOT:
        live leases existed and none could be attributed to a session, so zero here
        means "something is running and we cannot say what".
        """
        if self.source in ("leases", "activity", "disabled"):
            return True
        return self.source == "none" and not self.error


# Hermes writes the generic identity "custom" into billing_provider for every
# named custom provider, so it names no provider at all.
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


def provider_of(billing_provider: Any, model_config: Any) -> str:
    """The provider a session runs on, or '' when unknowable.

    ``model_config.provider`` wins because it holds the durable config key
    (``clinepass``), while ``billing_provider`` holds the runtime identity, which
    is the generic ``custom`` for any named custom provider. Grouping on
    ``billing_provider`` alone filed those sessions under a provider named
    "custom" that no cap is ever declared for, so a cap on the real provider could
    never see them. ``placement.py`` reads the same rule, from here, so the two
    modules cannot disagree about the same session.
    """
    for candidate in (_config_provider(model_config), billing_provider):
        name = str(candidate or "").strip()
        if name and name != _GENERIC_PROVIDER:
            return name
    return ""


def _tally(rows: Any) -> tuple[dict[str, int], str]:
    """Count rows per provider. Returns (counts, note about unreadable rows)."""
    counts: dict[str, int] = {}
    unknown = 0
    for _id, _key, billing, model_config in rows:
        name = provider_of(billing, model_config)
        if not name:
            unknown += 1
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts, (f"{unknown} session(s) with no readable provider" if unknown else "")


def _store_path(session_db: Optional[Path] = None) -> Path:
    if session_db is not None:
        return Path(session_db)
    # Hermes-home aware: a bare ~/.hermes misreads state under HERMES_HOME.
    from paths import state_db
    return state_db()


def active_by_provider(
    *,
    session_db: Optional[Path] = None,
    lease_grace_seconds: float = 300.0,
    activity_window_seconds: float = 300.0,
    now: Optional[float] = None,
) -> Load:
    """Count sessions doing work per provider.

    Prefers live turn leases; falls back to recent-activity counts. Returns an
    empty Load on any failure, because a load reading is an optimisation and
    must never break routing.
    """
    import time

    now = time.time() if now is None else now
    path = _store_path(session_db)
    if not path.exists():
        return Load({}, "no-store", f"no session store at {path}")

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return Load({}, "unreadable", f"cannot open {path}: {exc}")

    failures: list[str] = []
    lease_rows: list[Any] = []
    try:
        # Leases held by a session, still valid or acquired recently. A lease
        # that expired long ago is history, not load.
        lease_rows = list(con.execute(
            """
            select l.conversation_id, l.acquired_at, l.expires_at
            from session_turn_leases l
            """
        ))
    except sqlite3.Error as exc:
        failures.append(f"session_turn_leases: {exc}")

    counts: dict[str, int] = {}
    source = "none"
    if lease_rows:
        ids: list[str] = []
        for conversation_id, acquired_at, expires_at in lease_rows:
            lease_live = bool(expires_at) and expires_at >= now
            acquired_recently = bool(acquired_at) and (now - acquired_at) <= lease_grace_seconds
            if lease_live or acquired_recently:
                ids.append(str(conversation_id))
        if ids:
            source = "leases"
            placeholders = ",".join("?" * len(ids))
            # The lease stores conversation_id (the session_key); `sessions.id`
            # is the runtime id. Match either so a schema shift degrades to
            # fewer matches rather than a hard error.
            try:
                rows = con.execute(
                    f"select id, session_key, billing_provider, model_config from sessions "
                    f"where id in ({placeholders}) or session_key in ({placeholders})",
                    tuple(ids) + tuple(ids),
                )
                counts, note = _tally(rows)
                if note:
                    failures.append(note)
            except sqlite3.Error as exc:
                failures.append(f"sessions by lease: {exc}")
                counts = {}

    if not counts and source != "leases":
        # No live leases at all (older Hermes, or a surface that does not lease).
        # Fall back to recent activity, which is a weaker signal: a session that
        # finished a turn recently still occupies its provider's connection briefly.
        #
        # Deliberately NOT used when live leases existed but matched no session row:
        # that is a knowledge gap about WORK IN FLIGHT, and answering it with
        # recently-active sessions would charge idle conversations against a cap
        # that measures requests in flight. That case is reported as unknown below.
        try:
            rows = con.execute(
                "select id, session_key, billing_provider, model_config from sessions "
                "where last_activity_at > ?",
                (now - activity_window_seconds,),
            )
            counts, note = _tally(rows)
            if counts:
                source = "activity"
            if note:
                failures.append(note)
        except sqlite3.Error as exc:
            failures.append(f"sessions by activity: {exc}")

    if lease_rows and not counts and source == "leases":
        # Live leases that resolve to no session: something is running and this
        # reading cannot say what. Unknown, not zero, and not a substitute count.
        failures.append(f"{len(lease_rows)} live lease(s) matched no session row")

    con.close()
    detail = "; ".join(failures)[:200]
    if failures and not counts:
        # Nothing could be read, OR something is running that this reading cannot
        # attribute. An empty count here is ignorance, not idleness, and the caller
        # has to be able to tell which.
        return Load({}, "unreadable" if not lease_rows else "leases-unknown", detail)
    if not counts:
        # The store was read and nothing is running: a real zero, not a failure.
        source = "none"
    return Load(counts, source, detail)


def _as_cap(value: Any) -> Optional[int]:
    """A concurrency limit as a whole number, or None when it cannot be read as one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if float(value).is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def normalize_caps(caps: Any) -> tuple[dict[str, int], list[str]]:
    """Readable caps, and the entries that are not. The one reader for all modules.

    Each module used to read a cap its own way: a quoted `'3'` (an ordinary YAML
    typo) crashed the scorer with a TypeError, while `placement.plan` enforced it
    and the OVER CAP line ignored it, so a limit one part of the tool honoured
    another could not see. Absent and 0 keep meaning "no declared cap", which is a
    real configuration; anything present but unreadable is reported instead of
    being treated as unlimited.
    """
    if caps in (None, {}):
        return {}, []
    if not isinstance(caps, dict):
        return {}, [f"the caps block must map a provider to a limit, found {type(caps).__name__}"]
    out: dict[str, int] = {}
    problems: list[str] = []
    for name, value in caps.items():
        if isinstance(value, bool):
            # True/False read as 1/0 to Python; a boolean cap is a mistake either way.
            problems.append(f"{name}: {value!r}")
            continue
        if value is None:
            continue  # no declared cap
        number = _as_cap(value)
        if number is None:
            problems.append(f"{name}: {value!r}")
            continue
        if number == 0:
            continue  # "0 = no declared cap", in either spelling: 0 and '0'
        if number < 0:
            problems.append(f"{name}: {value!r}")
            continue
        out[str(name)] = number
    return out, problems


def over_capacity(load: Load, caps: dict[str, int]) -> dict[str, int]:
    """Providers over their concurrency cap, with the overflow count.

    A provider with no declared cap is never over it. Caps are normalised first, so
    a quoted limit is honoured here as well as in the planner.
    """
    readable, _problems = normalize_caps(caps)
    return {
        name: load.count(name) - cap
        for name, cap in readable.items()
        if load.count(name) > cap
    }
