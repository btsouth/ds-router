"""Quota polling and provider scoring.

Every figure here comes from a provider's own usage endpoint. Nothing is
estimated from local token counts, because a local count cannot see windows
that opened before this process started.
"""

from __future__ import annotations

import json
import re
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

    def __post_init__(self) -> None:
        # Enforce at the boundary, not only in the parsers. A percentage that
        # cannot be trusted must never be rewritten into a number the scorer reads
        # as healthy: 0.0 is simultaneously the lowest risk and the best headroom,
        # so turning NaN into 0 made a broken reading the most attractive provider
        # on the board. Invalid input raises here, and every parser drops such a
        # window before constructing it, which turns a bad reading into an
        # unreadable provider instead of a healthy-looking one.
        if _finite_percent(self.percent) is None:
            raise ValueError(
                f"window {self.label!r} carries an unusable percentage "
                f"({self.percent!r}); a window that cannot be trusted must not be scored")
        self.percent = float(self.percent)

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


def _http_json(url: str, key: str, timeout: float = 12.0) -> Any:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _finite_percent(value: Any) -> Optional[float]:
    """Coerce a percentage into a usable fraction, or None when unusable.

    Guards against NaN/inf, which arrive from a broken upstream reading. A NaN
    compares false against every threshold, so an unsanitized NaN window reads as
    *perfect headroom* and the provider looks like the healthiest option -- the
    worst possible failure direction.

    A negative value is unusable rather than zero. A provider cannot report
    negative usage, so a negative number is either a broken reading or a sentinel
    for "unknown", and clamping it to 0 hands the same best-possible score to a
    reading nobody can trust. Callers drop the window, and when nothing survives
    the provider reads as unreadable instead of empty.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool):
        # float(False) is 0.0 and float(True) is 1.0, so a garbled boolean field
        # would become the emptiest possible window (or a full one). Every other
        # numeric reader in this project rejects bools; so does this one now.
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return number if number >= 0.0 else None


# Fractional seconds beyond microsecond precision, which `datetime.fromisoformat`
# refused before Python 3.11.
_EXTRA_FRACTION = re.compile(r"(\.\d{6})\d+")

# The short numeric offset ("+0000") and the basic date form ("20260918T090635"),
# both of which `datetime.fromisoformat` rejects before Python 3.11. A provider
# changing its encoding must not silently cost us a reset time.
_ISO_SHORT_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")
_ISO_BASIC = re.compile(r"^(\d{4})(\d{2})(\d{2})(T\d{2})(\d{2})(\d{2})")


def _present(value: Any) -> bool:
    """True when a field carries something, as opposed to being absent or zero."""
    return value not in (None, "", 0)


def _reset(value: Any) -> Optional[float]:
    """Normalise epoch seconds, epoch milliseconds, or an ISO string.

    Returns None both when the value is absent and when it cannot be read. Callers
    that must tell those apart use ``_present``: an absent reset is normal, since
    Ollama publishes none, while a reset that is PRESENT and unreadable means the
    window cannot be reasoned about and is dropped rather than silently losing its
    pace and exhaustion rules.
    """
    if not _present(value):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        # Milliseconds are the only numeric encoding any provider sends. The
        # threshold is a magnitude test and not a calendar claim: 1e11 seconds is
        # year 5138, while every millisecond epoch is around 1.7e12.
        return seconds / 1000.0 if seconds > 1e11 else seconds
    try:
        from datetime import datetime

        text = str(value).strip().replace("Z", "+00:00")
        # ClinePass publishes nanosecond precision ("2026-09-18T09:06:35.170792893Z")
        # and datetime.fromisoformat rejected anything finer than microseconds
        # before Python 3.11. Passing that straight through raised, and the except
        # below turned a perfectly readable reset time into None -- so on 3.10 the
        # window lost its reset silently and pace/hard-exhaustion rules quietly
        # degraded. Trim the extra digits instead: three digits of a reset time are
        # worth nothing, the reset time itself is load-bearing.
        text = _EXTRA_FRACTION.sub(r"\1", text)
        text = _ISO_SHORT_OFFSET.sub(r"\1:\2", text)
        basic = _ISO_BASIC.match(text)
        if basic:
            year, month, day, hour, minute, second = basic.groups()
            text = f"{year}-{month}-{day}{hour}:{minute}:{second}" + text[basic.end():]
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def parse_commandcode(payload: dict) -> list[Window]:
    """GOAT/Pro windows carried by the credits endpoint: five hour and weekly.

    The monthly window needs this period's spend, which lives in a second
    endpoint, so it is added by ``fetch_quota`` once that figure is known. It is
    deliberately NOT created here as a zero placeholder: a window that was never
    read must be absent from the reading, because "0% used" is the emptiest and
    safest value the scorer knows, and a fabricated one wins the decision.
    """
    windows = payload.get("windowLimits")
    if not isinstance(windows, dict):
        raise ValueError("commandcode returned no windowLimits block")
    out: list[Window] = []
    for name, label in (("fiveHour", "session"), ("weekly", "weekly")):
        window = windows.get(name)
        if not isinstance(window, dict):
            continue
        used, cap = window.get("used"), window.get("cap")
        if not isinstance(used, (int, float)) or not isinstance(cap, (int, float)) or cap <= 0:
            continue
        fraction = _finite_percent(used / cap)
        if fraction is None:
            continue
        raw_reset = window.get("resetAt")
        reset = _reset(raw_reset)
        if reset is None and _present(raw_reset):
            continue  # a reset we cannot read is not a window we can reason about
        out.append(Window(label, fraction, reset))
    if not out:
        raise ValueError("commandcode returned no recognised windows")
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
        fraction = _finite_percent(window.get("percent"))
        if fraction is None:
            continue
        raw_reset = window.get("resetsAt")
        reset = _reset(raw_reset)
        if reset is None and _present(raw_reset):
            continue  # a reset we cannot read is not a window we can reason about
        out.append(Window(label, fraction / 100.0, reset))
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
        fraction = _finite_percent(window.get("usage"))
        if fraction is None:
            continue
        out.append(Window(str(name), fraction, None))
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
        fraction = _finite_percent(row.get("percentUsed"))
        if fraction is None:
            continue
        # Provider labels: five_hour / weekly / monthly. Normalize underscores so
        # the shared label mapper classifies them.
        label = str(row.get("type") or "").replace("_", " ")
        raw_reset = row.get("resetsAt")
        reset = _reset(raw_reset)
        if reset is None and _present(raw_reset):
            continue  # a reset we cannot read is not a window we can reason about
        out.append(Window(label, fraction / 100.0, reset))
    if not out:
        raise ValueError("clinepass returned no recognised windows")
    return out


_PARSERS: dict[str, Callable[[dict], list[Window]]] = {
    "opencode_go": parse_opencode_go,
    "ollama": parse_ollama,
}


# The windows each provider is expected to report. A response that omits one is not
# a smaller plan, it is a reading with a hole in it: the missing window cannot be
# reasoned about, and scoring the rest as if the provider had fewer limits is how a
# partially-read provider wins a destination on data nobody checked.
_EXPECTED_WINDOWS = {
    "commandcode": ("session", "weekly", "monthly"),
    "opencode-go": ("session", "weekly", "monthly"),
    "ollama-cloud": ("monthly",),
    "clinepass": ("session", "weekly", "monthly"),
}


def missing_windows(windows: list, provider: str) -> list[str]:
    """Window kinds *provider* is expected to report but did not."""
    expected = _EXPECTED_WINDOWS.get(str(provider))
    if not expected:
        return []
    have = {w.kind for w in windows if w.kind}
    return [kind for kind in expected if kind not in have]


def describe_windows(windows: list) -> str:
    """What was read, for a message that has to say what is missing AND what is not."""
    return ", ".join(f"{w.label or w.kind} {w.percent:.0%}" for w in windows) or "nothing"


def partial_note(provider: str, windows: list, *, skip: tuple = ()) -> str:
    """The "a window went missing" note, or '' when the reading looks complete.

    *skip* is for windows that legitimately come from another endpoint (CommandCode's
    monthly window is fetched separately, and has its own note).
    """
    missing = [kind for kind in missing_windows(windows, provider) if kind not in skip]
    if not missing:
        return ""
    return f"partial reading: no {', '.join(missing)} window (read: {describe_windows(windows)})"


def fetch_quota(provider: str, spec: dict, key: str, timeout: float = 12.0) -> Quota:
    """Read one provider's windows. A failure yields an error Quota, never raises."""
    kind = str(spec.get("quota") or "")
    try:
        if kind == "commandcode":
            base = "https://api.commandcode.ai"
            credits = _http_json(f"{base}/alpha/billing/credits", key, timeout)
            windows = parse_commandcode(credits)
            # The monthly window needs the period's spend, which lives in a
            # second endpoint. When that figure cannot be read, the reading is
            # incomplete rather than healthy: the monthly cap is the window that
            # actually throttles a GOAT plan, so a provider whose monthly usage is
            # unknown must not be chosen as a new destination. It says so in
            # `note`, which makes the quota stale, which is exactly the "readings
            # gate destinations, exhaustion gates keeping" rule -- sessions
            # already there stay put.
            note = ""
            try:
                spent = _http_json(f"{base}/alpha/usage/summary", key, timeout).get("totalCredits")
            except Exception as exc:
                spent, note = None, f"monthly window unread: {type(exc).__name__}"
            monthly = (credits.get("credits") or {}).get("monthlyCredits")
            if not note:
                if isinstance(spent, (int, float)) and isinstance(monthly, (int, float)) and (monthly + spent) > 0:
                    windows.append(Window("monthly", spent / (monthly + spent), None))
                else:
                    note = ("monthly window unread: the spend summary carried no usable "
                            "credit figures")
            # Every window the credits payload was supposed to carry, on top of the
            # monthly one handled above.
            if note == "":
                note = partial_note(provider, windows, skip=("monthly",))
            if note:
                # " (read: " and not "read:", which the note's own "unread:" would match.
                note = note if " (read: " in note else f"{note} (read: {describe_windows(windows)})"
            plan = ""
            try:
                data = _http_json(f"{base}/alpha/billing/subscriptions", key, timeout).get("data") or {}
                plan_id = data.get("planId")
                if isinstance(plan_id, str) and plan_id:
                    plan = plan_id.split("-")[-1].upper()
            except Exception:
                # The plan name is cosmetic (it only labels the output). A failure
                # here must not discard the windows that were read successfully.
                pass
            return Quota(provider, windows, time.time(), note, plan)

        if kind == "opencode_go":
            payload = _http_json("https://opencode.ai/zen/go/v1/usage", key, timeout)
            windows = _PARSERS["opencode_go"](payload)
            return Quota(provider, windows, time.time(), partial_note(provider, windows))

        if kind == "ollama":
            payload = _http_json("https://ollama.com/api/usage", key, timeout)
            plan = payload.get("plan") if isinstance(payload.get("plan"), str) else ""
            windows = _PARSERS["ollama"](payload)
            return Quota(provider, windows, time.time(), partial_note(provider, windows), plan)

        if kind == "clinepass":
            payload = _http_json("https://api.cline.bot/api/v1/users/me/plan/usage-limits", key, timeout)
            windows = parse_clinepass(payload)
            plan = ""
            try:
                meta = _http_json("https://api.cline.bot/api/v1/users/me/plan", key, timeout)
                data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
                plan = str((data.get("plan") or {}).get("displayName") or "")
            except Exception:
                # Cosmetic label only; never fail the quota read over it.
                pass
            return Quota(provider, windows, time.time(), partial_note(provider, windows), plan)

        return Quota(provider, [], time.time(), f"no quota reader for {kind!r}")
    except Exception as exc:
        # Never surface a response body: it can carry credential-bearing fields.
        return Quota(provider, [], time.time(), f"{type(exc).__name__}: {exc}"[:200])


COLLECTOR_FILES = {
    "commandcode": "commandcode-quota.json",
    "opencode-go": "go-quota.json",
    "ollama-cloud": "ollama-quota.json",
    # Written by a snapshot cache once it covers ClinePass. Absent is fine: the
    # caller polls ClinePass directly.
    "clinepass": "clinepass-quota.json",
}


def from_collector(provider: str, state_dir: "Path", ttl_seconds: float = 300.0) -> Optional[Quota]:
    """Build a Quota from a cached snapshot file, if one is present and fresh.

    Something else on the machine already polls these same endpoints on its own
    schedule, so reading its snapshot avoids asking the usage APIs twice for one
    answer. An absent or stale file returns None and the caller polls directly.
    Returns None when the snapshot is missing, stale, or errored, in which case
    the caller polls directly.
    """
    filename = COLLECTOR_FILES.get(provider)
    if not filename:
        return None
    # expanduser is idempotent, so a caller may pass either a raw config path or
    # one already resolved.
    path = Path(state_dir).expanduser() / filename
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        # Valid JSON that is not an object ([1,2], "x", 3, null) used to raise
        # AttributeError from payload.get below, which took the whole router down
        # with a traceback rather than reading as "no usable snapshot".
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
        fraction = _finite_percent(row.get("percent"))
        if fraction is None:
            continue
        raw_reset = row.get("resetsAt")
        reset = _reset(raw_reset)
        if reset is None and _present(raw_reset):
            continue  # a reset we cannot read is not a window we can reason about
        windows.append(Window(str(row.get("label") or ""), fraction, reset))
    if not windows:
        return None
    if missing_windows(windows, provider):
        # A snapshot missing a window must not become the reading: poll the real
        # endpoint instead, which is one request and answers completely.
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
