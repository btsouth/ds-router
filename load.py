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

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Load:
    """Active session counts per provider, plus how the figure was obtained."""

    counts: dict[str, int] = field(default_factory=dict)
    source: str = "none"

    def count(self, provider: str) -> int:
        return int(self.counts.get(provider, 0))

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _store_path(session_db: Optional[Path] = None) -> Path:
    if session_db is not None:
        return Path(session_db)
    return Path.home() / ".hermes" / "state.db"


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
        return Load({}, "no-store")

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return Load({}, "unreadable")

    try:
        # Leases held by a session, still valid or acquired recently. A lease
        # that expired long ago is history, not load.
        lease_rows = list(con.execute(
            """
            select l.conversation_id, l.acquired_at, l.expires_at
            from session_turn_leases l
            """
        ))
    except sqlite3.Error:
        lease_rows = []

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
                    f"select billing_provider, count(*) from sessions "
                    f"where id in ({placeholders}) or session_key in ({placeholders}) "
                    f"group by billing_provider",
                    tuple(ids) + tuple(ids),
                )
                counts = {(p or "(unknown)"): int(n) for p, n in rows}
            except sqlite3.Error:
                counts = {}

    if not counts:
        # No lease data (or no leases held). Fall back to recent activity.
        try:
            rows = con.execute(
                "select billing_provider, count(*) from sessions "
                "where last_activity_at > ? group by billing_provider",
                (now - activity_window_seconds,),
            )
            counts = {(p or "(unknown)"): int(n) for p, n in rows}
            if counts:
                source = "activity"
        except sqlite3.Error:
            pass

    con.close()
    return Load(counts, source)


def over_capacity(load: Load, caps: dict[str, int]) -> dict[str, int]:
    """Providers over their concurrency cap, with the overflow count.

    A provider with no declared cap is never over it.
    """
    return {
        name: load.count(name) - cap
        for name, cap in caps.items()
        if isinstance(cap, int) and cap > 0 and load.count(name) > cap
    }
