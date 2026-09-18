"""Quota polling and provider scoring.

Every figure here comes from a provider's own usage endpoint. Nothing is
estimated from local token counts, because a local count cannot see windows
that opened before this process started.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

USER_AGENT = "ds-router/0.1"

# Window label -> weight key. Providers name their windows differently; this is
# the only place that naming is normalised.
_WINDOW_KIND = {
    "session": "session",
    "rolling": "session",
    "5 hours": "session",
    "5-hour": "session",
    "weekly": "weekly",
    "weekly (7-day)": "weekly",
    "monthly": "monthly",
}


def _kind(label: str) -> Optional[str]:
    """Map a provider's window label onto session/weekly/monthly.

    Labels vary by source: the same OpenCode window arrives as "session" from
    its own API and as "Session (5-hour)" from the collector snapshot. Matching
    on substrings rather than exact strings keeps one window from behaving
    differently depending on where the reading came from.
    """
    text = str(label or "").strip().lower()
    if not text:
        return None
    if exact := _WINDOW_KIND.get(text):
        return exact
    # Strip a parenthetical qualifier and retry the exact table.
    if "(" in text:
        stripped = text.split("(", 1)[0].strip()
        if exact := _WINDOW_KIND.get(stripped):
            return exact
    for needle, kind in (("week", "weekly"), ("month", "monthly"),
                         ("session", "session"), ("rolling", "session"),
                         ("hour", "session")):
        if needle in text:
            return kind
    return None

# Nominal window lengths, needed to compare a window's usage against how much
# of it has elapsed. Providers do not publish these per response, so they live
# here. Ollama's monthly window carries no reset time and therefore no pace.
_WINDOW_SECONDS = {
    "session": 5 * 3600.0,
    "weekly": 7 * 86400.0,
    "monthly": 30 * 86400.0,
}

# Elapsed time is never assumed to be less than this fraction of a window when
# extrapolating a burn rate, so a fresh window's first burst cannot project
# into a fake emergency.
_MIN_ELAPSED_FRACTION = 0.15


@dataclass
class Window:
    """One limit window as a fraction used, 0.0-1.0+ (overage is possible)."""

    label: str
    percent: float
    resets_at: Optional[float] = None

    @property
    def kind(self) -> Optional[str]:
        return _kind(self.label)

    @property
    def nominal_seconds(self) -> Optional[float]:
        kind = self.kind
        return _WINDOW_SECONDS.get(kind) if kind else None

    def pace(self, now: Optional[float] = None) -> Optional[float]:
        """Burn rate against the window's own refill rate.

        1.0 means "on pace to just barely finish the window"; above 1.0 means
        the window will run out before it resets. This is what makes two
        numbers comparable at all: 45% of a month is a slow burn, while 45% of
        a 5-hour window that resets in 24 minutes is nothing to worry about.

        Elapsed time is floored at a fraction of the window before the rate is
        extrapolated. Without that floor a window that just reset reads as a
        catastrophe: 9% used in the first day of a 30-day window extrapolates
        to a 270% burn, when in reality it is one burst of usage at the start
        of a fresh allowance. The floor says "assume at least this much of the
        window has passed" so early usage cannot be projected so aggressively.

        None when the window has no reset time or no known length, so the
        caller falls back to the raw fraction instead of inventing elapsed
        time.
        """
        length = self.nominal_seconds
        if not length or self.resets_at is None:
            return None
        now = time.time() if now is None else now
        remaining = self.resets_at - now
        # A reset time beyond the window's own length means the reading is
        # wrong (clock skew, a mis-set plan boundary, a provider changing its
        # window definition). Extrapolating from it would invent a crisis, so
        # treat the pace as unknowable instead.
        if remaining > length:
            return None
        remaining = max(0.0, remaining)
        elapsed = max(0.0, min(length, length - remaining))
        effective = max(elapsed, _MIN_ELAPSED_FRACTION * length)
        if effective <= 0:
            return self.percent
        return self.percent / (effective / length)


@dataclass
class Quota:
    """One provider's windows, plus whether the reading is usable."""

    provider: str
    windows: list[Window] = field(default_factory=list)
    read_at: float = 0.0
    error: str = ""
    plan: str = ""

    @property
    def stale(self) -> bool:
        return bool(self.error) or not self.windows

    def pressure(self, weights: dict[str, float], reset_soon_seconds: float = 1800.0,
                 now: Optional[float] = None) -> float:
        """How close this provider is to interrupting a session.

        The tightest weighted window decides, not the average: a plan at 98%
        weekly is spent even if its monthly window sits at 10%.

        A window that resets within *reset_soon_seconds* contributes nothing,
        because a near-full window that refreshes in twenty minutes cannot
        interrupt anything. Without this the router would flee a provider that
        is about to hand back a full allowance.
        """
        if not self.windows:
            return 0.0
        now = time.time() if now is None else now
        session_weight = weights.get("session", 1.0)
        scored = []
        for window in self.windows:
            # An unrecognised window name is treated as session-class risk
            # rather than ignored, so a provider renaming a window fails safe
            # (skipped early) instead of looking permanently healthy.
            kind = window.kind
            weight = weights.get(kind, session_weight) if kind else session_weight
            due = window.resets_at
            if due is not None and 0 < (due - now) <= reset_soon_seconds:
                continue
            scored.append(window.percent * weight)
        return max(scored) if scored else 0.0


def _http_json(url: str, key: str, timeout: float = 12.0) -> Any:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _reset(value: Any) -> Optional[float]:
    """Normalise epoch seconds, epoch milliseconds, or an ISO string."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        # Anything past year 2286 in seconds is really milliseconds.
        return seconds / 1000.0 if seconds > 1e11 else seconds
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_commandcode(payload: dict) -> list[Window]:
    """GOAT/Pro windows: dollar-denominated, with real reset times."""
    windows = payload.get("windowLimits") or {}
    out: list[Window] = []
    for name, label in (("fiveHour", "session"), ("weekly", "weekly")):
        window = windows.get(name)
        if not isinstance(window, dict):
            continue
        used, cap = window.get("used"), window.get("cap")
        if not isinstance(used, (int, float)) or not isinstance(cap, (int, float)) or cap <= 0:
            continue
        out.append(Window(label, max(0.0, used / cap), _reset(window.get("resetAt"))))
    monthly = (payload.get("credits") or {}).get("monthlyCredits")
    if isinstance(monthly, (int, float)):
        # The endpoint reports remaining credits; the spent figure is not
        # carried here, so express headroom as the fraction already consumed of
        # what is left plus what this period spent (filled in by the caller).
        out.append(Window("monthly", 0.0, None))
        out[-1].percent = 0.0
    return out


def parse_opencode_go(payload: dict) -> list[Window]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("opencode-go returned no usage block")
    out: list[Window] = []
    for name, label in (("rolling", "session"), ("weekly", "weekly"), ("monthly", "monthly")):
        window = usage.get(name)
        if not isinstance(window, dict):
            continue
        percent = window.get("percent")
        if not isinstance(percent, (int, float)):
            continue
        out.append(Window(label, max(0.0, float(percent) / 100.0), _reset(window.get("resetsAt"))))
    if not out:
        raise ValueError("opencode-go returned no recognised windows")
    return out


def parse_ollama(payload: dict) -> list[Window]:
    limits = payload.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("ollama returned no limits block")
    out: list[Window] = []
    for name in limits:
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        usage = window.get("usage")
        if not isinstance(usage, (int, float)):
            continue
        out.append(Window(str(name), max(0.0, float(usage)), None))
    if not out:
        raise ValueError("ollama returned no recognised windows")
    return out


def parse_clinepass(payload: dict) -> list[Window]:
    """ClinePass: five_hour / weekly / monthly, all percent-denominated.

    The response is wrapped in {"data": {"limits": [...]}}. Types arrive as
    ``five_hour`` (underscore), which the label mapper already handles via the
    "hour" substring rule.
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    limits = data.get("limits")
    if not isinstance(limits, list):
        raise ValueError("clinepass returned no limits array")
    out: list[Window] = []
    for row in limits:
        if not isinstance(row, dict):
            continue
        percent = row.get("percentUsed")
        if not isinstance(percent, (int, float)):
            continue
        # Provider labels: five_hour / weekly / monthly. Normalize underscores so
        # the shared label mapper classifies them.
        label = str(row.get("type") or "").replace("_", " ")
        out.append(Window(label, max(0.0, float(percent) / 100.0), _reset(row.get("resetsAt"))))
    if not out:
        raise ValueError("clinepass returned no recognised windows")
    return out


_PARSERS: dict[str, Callable[[dict], list[Window]]] = {
    "opencode_go": parse_opencode_go,
    "ollama": parse_ollama,
}


def fetch_quota(provider: str, spec: dict, key: str, timeout: float = 12.0) -> Quota:
    """Read one provider's windows. A failure yields an error Quota, never raises."""
    kind = str(spec.get("quota") or "")
    try:
        if kind == "commandcode":
            base = "https://api.commandcode.ai"
            credits = _http_json(f"{base}/alpha/billing/credits", key, timeout)
            windows = parse_commandcode(credits)
            # The monthly window needs the period's spend, which lives in a
            # second endpoint. Without it the monthly window is reported as
            # unknown rather than guessed.
            try:
                spent = _http_json(f"{base}/alpha/usage/summary", key, timeout).get("totalCredits")
                monthly = (credits.get("credits") or {}).get("monthlyCredits")
                if isinstance(spent, (int, float)) and isinstance(monthly, (int, float)) and (monthly + spent) > 0:
                    windows = [w for w in windows if w.label != "monthly"]
                    windows.append(Window("monthly", spent / (monthly + spent), None))
            except Exception:
                windows = [w for w in windows if w.label != "monthly"]
            plan = ""
            try:
                data = _http_json(f"{base}/alpha/billing/subscriptions", key, timeout).get("data") or {}
                plan_id = data.get("planId")
                if isinstance(plan_id, str) and plan_id:
                    plan = plan_id.split("-")[-1].upper()
            except Exception:
                pass
            return Quota(provider, windows, time.time(), "", plan)

        if kind == "opencode_go":
            payload = _http_json("https://opencode.ai/zen/go/v1/usage", key, timeout)
            return Quota(provider, _PARSERS["opencode_go"](payload), time.time())

        if kind == "ollama":
            payload = _http_json("https://ollama.com/api/usage", key, timeout)
            plan = payload.get("plan") if isinstance(payload.get("plan"), str) else ""
            return Quota(provider, _PARSERS["ollama"](payload), time.time(), "", plan)

        if kind == "clinepass":
            payload = _http_json("https://api.cline.bot/api/v1/users/me/plan/usage-limits", key, timeout)
            plan = ""
            try:
                meta = _http_json("https://api.cline.bot/api/v1/users/me/plan", key, timeout)
                data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
                plan = str((data.get("plan") or {}).get("displayName") or "")
            except Exception:
                pass
            return Quota(provider, parse_clinepass(payload), time.time(), "", plan)

        return Quota(provider, [], time.time(), f"no quota reader for {kind!r}")
    except Exception as exc:
        # Never surface a response body: it can carry credential-bearing fields.
        return Quota(provider, [], time.time(), f"{type(exc).__name__}: {exc}"[:200])


COLLECTOR_FILES = {
    "commandcode": "commandcode-quota.json",
    "opencode-go": "go-quota.json",
    "ollama-cloud": "ollama-quota.json",
    # Written by the omarchy-usage-dashboard collector once ClinePass is wired
    # in there. Absent is fine: the caller polls ClinePass directly.
    "clinepass": "clinepass-quota.json",
}


def from_collector(provider: str, state_dir: "Path", ttl_seconds: float = 300.0) -> Optional[Quota]:
    """Build a Quota from the omarchy-usage-dashboard collector's snapshot.

    That collector already polls these same endpoints on its own schedule, so
    reading its snapshot avoids hammering the usage APIs twice for one answer.
    Returns None when the snapshot is missing, stale, or errored, in which case
    the caller polls directly.
    """
    filename = COLLECTOR_FILES.get(provider)
    if not filename:
        return None
    path = Path(state_dir).expanduser() / filename
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if payload.get("error"):
        return None
    attempted = payload.get("attemptedAt")
    if not isinstance(attempted, (int, float)) or (time.time() - attempted) > ttl_seconds:
        return None
    windows: list[Window] = []
    for row in payload.get("limits") or []:
        if not isinstance(row, dict):
            continue
        percent = row.get("percent")
        if not isinstance(percent, (int, float)):
            continue
        windows.append(Window(str(row.get("label") or ""), float(percent), _reset(row.get("resetsAt"))))
    if not windows:
        return None
    return Quota(provider, windows, float(attempted), "", str(payload.get("plan") or ""))


def load_key(spec: dict, env: dict[str, str]) -> str:
    return str(env.get(str(spec.get("key_env") or ""), "")).strip()


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            env[name.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env
