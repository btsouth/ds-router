"""Time-of-day pricing.

CommandCode and Ollama both charge peak rates, at windows that barely overlap,
so preferring the off-peak provider is free efficiency: no cache reset, no
quality change, no context cost. It is the only optimisation here that is
strictly a win.

Hours are UTC and weekdays only, matching both providers' published windows.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone


def _weekday_hour(now: float | None = None) -> tuple[int, int]:
    now = time.time() if now is None else now
    moment = datetime.fromtimestamp(now, timezone.utc)
    return moment.weekday(), moment.hour  # Monday == 0


def _span(window: object) -> tuple[int, int]:
    """One peak window as (start_hour, end_hour).

    A malformed entry raises rather than being skipped. Skipping it silently said
    "this provider is never at peak", so a config written as ``[12, 18]`` instead
    of ``[[12, 18]]`` made the router prefer that provider for the whole peak
    window on the strength of a claim it had just failed to read.
    """
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        raise ValueError(f"peak window {window!r} must be [start_hour, end_hour]")
    try:
        start, end = int(window[0]), int(window[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"peak window {window!r} must hold two whole hours") from exc
    if not (0 <= start < end <= 24):
        raise ValueError(f"peak window {window!r} must satisfy 0 <= start < end <= 24")
    return start, end


def in_peak(windows: list, now: float | None = None) -> bool:
    """True when *now* falls inside any [start_hour, end_hour) weekday window."""
    if not windows:
        return False
    spans = [_span(window) for window in windows]
    weekday, hour = _weekday_hour(now)
    if weekday > 4:  # Saturday, Sunday
        return False
    return any(start <= hour < end for start, end in spans)


def peak_providers(peak_cfg: dict, now: float | None = None) -> set[str]:
    """Names of providers currently billing at peak rates."""
    windows = (peak_cfg or {}).get("windows") or {}
    return {name for name, spans in windows.items() if in_peak(spans, now)}
