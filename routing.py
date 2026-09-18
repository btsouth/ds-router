"""Provider selection.

Three ideas carry the whole design:

1. Compare burn rate, not raw percent. A window is dangerous when it will run
   out before it refills, so usage is compared against how much of the window
   has elapsed. This is what makes "47% of a 5-hour window" and "45% of a
   month" answerable in the same units.

2. Separate hard exhaustion from soft pressure, because they deserve opposite
   responses. A provider that has actually run out must be left immediately,
   whatever stickiness costs. A provider that is merely burning fast is a
   prediction, and a wrong prediction costs a cache reset, so it is not worth
   moving for on its own.

3. Stay put until there is a reason. A switch resets the upstream prompt cache
   and drops the model's reasoning traces, so moving is only correct when the
   current provider is exhausted or predicted to die mid-turn. A provider whose
   reading failed is not "unreadable" in that sense: it simply cannot win, because
   a real reading outranks a missing one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from quota import Quota


@dataclass
class Candidate:
    provider: str
    model_id: str
    pressure: float          # exhaustion risk; 0.0 is safe. Primary key.
    headroom: float = 9.9    # fullest window as a raw fraction. Tie-break only.
    quota_ok: bool = True
    hard: bool = False
    detail: str = ""
    soonest_reset: Optional[float] = None

    @property
    def rank_key(self) -> tuple:
        """Sort key: unreadable last, then least risk, then most headroom.

        The provider name is the final tiebreak. Without it a tie resolves by
        dict insertion order, so the same fleet could pick a different provider
        depending on how the config happened to be ordered -- which would make
        `--plan` output unreproducible.
        """
        return (not self.quota_ok, self.pressure, self.headroom, self.provider)


@dataclass
class Decision:
    provider: str
    model_id: str
    reason: str
    ranked: list[Candidate] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.provider)


def hard_exhausted(quota: Quota, skip_at: float, now: Optional[float] = None) -> bool:
    """True when a window reports itself spent.

    `skip_at` is also a floor: a provider reporting >= skip_at with no reset
    time cannot be reasoned about for recovery, only for damage, so it counts
    as spent. A window whose reset time has already passed is ignored, because
    it has refilled upstream and the reading is stale.
    """
    now = time.time() if now is None else now
    for window in quota.windows:
        if window.resets_at is not None and window.resets_at <= now:
            continue  # already reset upstream
        if window.percent >= 1.0:
            return True
        if window.resets_at is None and window.percent >= skip_at:
            return True
    return False


def predicted_to_run_out(quota: Quota, deadline_seconds: float = 1800.0,
                         now: Optional[float] = None) -> bool:
    """True when the current burn rate exhausts a window within the deadline.

    This is the "will it die during this turn" test. 60% of a 5-hour window
    that resets in 20 minutes is fine (it refills soon). 60% of a week that
    resets in three days and is still climbing is not.
    """
    now = time.time() if now is None else now
    for window in quota.windows:
        pace = window.pace(now)
        if pace is None or pace <= 1.0:
            continue
        if window.resets_at is None or window.resets_at <= now:
            continue
        length = window.nominal_seconds or 0.0
        elapsed = max(1.0, length - max(0.0, window.resets_at - now))
        per_second = window.percent / elapsed
        if per_second <= 0:
            continue
        if max(0.0, 1.0 - window.percent) / per_second <= deadline_seconds:
            return True
    return False


def score(quota: Quota, model_id: Optional[str], weights: dict[str, float],
          skip_at: float, deadline_seconds: float = 1800.0,
          now: Optional[float] = None, refill_soon_seconds: float = 3600.0,
          active_sessions: int = 0, concurrency_cap: Optional[int] = None,
          pressure_per_over: float = 0.08, max_load_pressure: float = 0.4,
          health: Optional[dict] = None, slow_seconds: float = 10.0,
          slow_pressure: float = 0.25) -> Candidate:
    """Score one provider for one model.

    Two numbers, in order of importance:

    * ``risk`` — how likely this provider is to run out before a window
      refills. 0.0 means every window survives to its reset. This is the
      decision number.

    A provider over its concurrency cap gains risk, because queued requests are
    slow and a full queue is rejected. It is pressure rather than hard
    exhaustion: queueing still works, so the provider stays reachable when
    everything else is worse.
    * ``headroom`` — the fullest window as a raw fraction, used only to break
      ties between providers that are all safe, by preferring the emptier one.
      A window about to reset is excluded, because it is about to hand back a
      full allowance; counting it would make a provider look spent at the exact
      moment it becomes free.

    Comparing risk rather than raw percent is what stops a slow monthly burn
    from outranking a healthy short window. ``weights`` scales risk per window
    kind so a short window's overrun counts more than a monthly one's.
    """
    if model_id is None:
        return Candidate("", "", 9.9, 9.9, False, False, "model not offered here")
    if quota.stale:
        return Candidate(quota.provider, model_id, skip_at + 0.001, 9.9, False, False,
                         quota.error or "no reading")

    now = time.time() if now is None else now
    default_weight = weights.get("session", 1.0)
    shown: list[str] = []
    soonest: Optional[float] = None
    risk = 0.0
    headroom = 0.0
    about_to_refill = False

    for window in quota.windows:
        weight = weights.get(window.kind, default_weight) if window.kind else default_weight
        pace = window.pace(now)
        refills_soon = (window.resets_at is not None
                        and 0 < (window.resets_at - now) <= refill_soon_seconds)
        if pace is None:
            # No reset time, so no pace is computable. The raw fraction is the
            # only honest signal, and it must NOT be scaled down by weight:
            # that would make an unmeasurable window look permanently cheap.
            window_risk = window.percent
            shown.append(f"{window.label} {window.percent:.0%} (no reset time)")
        elif pace <= 1.0:
            # On pace to reach the reset with room to spare.
            window_risk = 0.0
            shown.append(f"{window.label} {window.percent:.0%} (on pace)")
        else:
            # Will run out before it refills; how far past is the shortfall.
            window_risk = (pace - 1.0) * weight
            shown.append(f"{window.label} {window.percent:.0%} (pace {pace:.2f}x)")
        risk = max(risk, window_risk)

        if refills_soon:
            about_to_refill = True
            continue  # about to be free; not a headroom concern
        headroom = max(headroom, window.percent)
        if window.resets_at and window.resets_at > now:
            soonest = window.resets_at if soonest is None else min(soonest, window.resets_at)

    if concurrency_cap and active_sessions > concurrency_cap:
        over = active_sessions - concurrency_cap
        load_risk = min(max_load_pressure, over * pressure_per_over)
        risk = max(risk, load_risk)
        shown.append(f"{active_sessions} active / cap {concurrency_cap} (+{over} queued)")

    # A provider that failed its health probe is treated as unusable rather than
    # merely unattractive: quota says what a plan allows, health says whether the
    # endpoint is answering at all, and an endpoint that is down cannot be spent
    # down. A provider that answered but slowly gains pressure, which only breaks
    # ties between otherwise-equal providers.
    unhealthy = False
    if health is not None:
        ok = health.get("ok") if isinstance(health, dict) else None
        if ok is False:
            unhealthy = True
            err = str(health.get("error") or "probe failed")[:60]
            shown.append(f"health probe FAILED ({err})")
        elif ok is True and isinstance(health.get("seconds"), (int, float)):
            elapsed = float(health["seconds"])
            if elapsed >= slow_seconds:
                risk = max(risk, min(slow_pressure, elapsed / max(slow_seconds, 1e-6) * slow_pressure))
                shown.append(f"slow ({elapsed:.1f}s)")

    hard = hard_exhausted(quota, skip_at, now) or unhealthy
    if predicted_to_run_out(quota, deadline_seconds, now):
        shown.append(f"expected to exhaust within {int(deadline_seconds // 60)}m")
    if about_to_refill and not shown:
        shown.append("window refilling shortly")
    return Candidate(quota.provider, model_id, risk, headroom, True, hard,
                     ", ".join(shown), soonest)


def choose(
    model_alias: str,
    providers: dict[str, dict],
    quotas: dict[str, Quota],
    weights: dict[str, float],
    skip_at: float,
    *,
    pinned: Optional[str] = None,
    sticky_provider: Optional[str] = None,
    deadline_seconds: float = 1800.0,
    now: Optional[float] = None,
    peak_providers: Optional[set[str]] = None,
    load: Optional[dict[str, int]] = None,
    concurrency_caps: Optional[dict[str, int]] = None,
    pressure_per_over: float = 0.08,
    max_load_pressure: float = 0.4,
    health: Optional[dict[str, dict]] = None,
) -> Decision:
    """Pick a provider for *model_alias*.

    Precedence: an explicit pin, then the conversation's current provider if it
    is usable, then the provider with the most headroom.

    Peak pricing is a TIE-BREAK, never a reason to abandon a healthy sticky
    provider. Doubling the per-token rate is not worth a cache reset, but it is
    worth choosing the cheaper of two equally safe providers.
    """
    active = load or {}
    caps = concurrency_caps or {}
    candidates: list[Candidate] = []
    for name, spec in providers.items():
        model_id = (spec.get("models") or {}).get(model_alias)
        candidate = score(quotas.get(name, Quota(name)), model_id, weights,
                          skip_at, deadline_seconds, now,
                          active_sessions=int(active.get(name, 0)),
                          concurrency_cap=caps.get(name),
                          pressure_per_over=pressure_per_over,
                          max_load_pressure=max_load_pressure,
                          health=(health or {}).get(name))
        # Identity comes from the providers mapping, which is the authority the
        # caller acts on. A Quota built for one provider but filed under another
        # would otherwise route to a name that does not exist.
        candidate.provider = name
        candidates.append(candidate)
    offered = [c for c in candidates if c.model_id]
    ranked = sorted(offered, key=lambda c: c.rank_key)
    on_peak = peak_providers or set()

    if not ranked:
        return Decision("", "", f"no provider serves {model_alias!r}", ranked)

    if pinned:
        match = next((c for c in ranked if c.provider == pinned), None)
        if match is None:
            return Decision("", "", f"pinned provider {pinned!r} does not serve {model_alias!r}", ranked)
        if match.quota_ok and not match.hard:
            return Decision(match.provider, match.model_id, f"pinned; {match.detail}", ranked)
        return Decision("", "", f"pinned provider {pinned!r} is exhausted ({match.detail})", ranked)

    if sticky_provider:
        match = next((c for c in ranked if c.provider == sticky_provider), None)
        # Stickiness tolerates soft pressure: a switch costs a cache reset and
        # drops reasoning, and a pace prediction can simply be wrong. It does
        # not tolerate hard exhaustion or an unreadable quota.
        if match and match.quota_ok and not match.hard:
            return Decision(match.provider, match.model_id, f"sticky; {match.detail}", ranked)

    safe = [c for c in ranked if c.quota_ok and not c.hard and c.pressure < skip_at]
    if not safe:
        best = ranked[0]
        # Never strand the agent. If nothing is readable or nothing is under the
        # line, hand back the best available option WITH a warning, because
        # "no provider" is always worse than the primary Hermes would have used
        # anyway. The caller decides whether to honour it.
        if best.model_id and not best.hard:
            return Decision(best.provider, best.model_id,
                            f"degraded; nothing safely readable, using {best.provider} ({best.detail or 'no reading'})",
                            ranked)
        detail = best.detail or "unreadable"
        return Decision("", "", f"all providers exhausted or unreadable (best: {best.provider} {detail})", ranked)

    # Among safe providers, prefer off-peak at the same risk level. "Same risk
    # level" is deliberately generous: any two providers that are both safe are
    # close enough that the cheaper rate should decide.
    off_peak = [c for c in safe if c.provider not in on_peak]
    chosen = off_peak[0] if off_peak else safe[0]
    reason = f"most headroom; {chosen.detail}"
    if off_peak and on_peak & {c.provider for c in safe}:
        reason += " (off-peak right now)"
    elif chosen.provider in on_peak:
        reason += " (all options at peak; picked the safest)"
    if sticky_provider and sticky_provider != chosen.provider:
        reason += f" (moved off {sticky_provider})"
    return Decision(chosen.provider, chosen.model_id, reason, ranked)


class StickyTable:
    """Conversation -> (provider, chosen_at). In memory; a restart re-picks."""

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._rows: dict[str, tuple[str, float]] = {}

    def get(self, conversation: str) -> Optional[str]:
        row = self._rows.get(conversation)
        if not row:
            return None
        provider, chosen_at = row
        # `>=`, not `>`: a row is good for at most ttl seconds, and a ttl of 0
        # must mean nothing is ever remembered. Reading the boundary strictly also
        # made the result depend on the host clock's resolution -- macOS can return
        # the same time.time() twice in a row, so a just-put row looked live.
        if time.time() - chosen_at >= self.ttl:
            self._rows.pop(conversation, None)
            return None
        return provider

    def put(self, conversation: str, provider: str) -> None:
        self._rows[conversation] = (provider, time.time())

    def forget(self, conversation: str) -> None:
        self._rows.pop(conversation, None)

    def evict_expired(self) -> None:
        now = time.time()
        for key in [k for k, (_, at) in self._rows.items() if now - at >= self.ttl]:
            self._rows.pop(key, None)
