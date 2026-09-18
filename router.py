#!/usr/bin/env python3
"""Dry-run the router: read live quotas, print the decision, proxy nothing.

This is the safe way to watch it think before trusting it with real traffic.

    ./router.py --dry-run
    ./router.py --dry-run --health        # also time a tiny request per provider
    ./router.py --list-models
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import load as load_mod  # noqa: E402
import peak as p  # noqa: E402
import quota as q  # noqa: E402
import routing as r  # noqa: E402

CONFIG = HERE / "config.yaml"
from paths import env_file as _hermes_env_file  # noqa: E402

HERMES_ENV = _hermes_env_file()


def load_config() -> dict:
    import yaml

    return yaml.safe_load(CONFIG.read_text())


def build_view(config: dict, env: dict[str, str]):
    """Flatten the config's model table into the shape routing.choose wants."""
    models = config.get("models") or {}
    providers: dict[str, dict] = {}
    for name, spec in (config.get("providers") or {}).items():
        spec = dict(spec)
        spec["models"] = {alias: table.get(name) for alias, table in models.items()
                          if isinstance(table, dict)}
        providers[name] = spec
    return providers


def _expanded(value: Any) -> Optional[Path]:
    """A config path with ~ resolved, or None when unset."""
    text = str(value or "").strip()
    return Path(text).expanduser() if text else None


def collect(providers: dict, env: dict[str, str], timeout: float = 12.0,
            routing_cfg: dict | None = None) -> dict[str, q.Quota]:
    """Read every provider's quota, preferring a fresh collector snapshot."""
    routing_cfg = routing_cfg or {}
    reuse = bool(routing_cfg.get("reuse_collector_state", True))
    # Expand here, at the boundary: the config holds a display path like
    # "~/.local/state/...", and a caller that forgot expanduser would silently
    # find nothing and poll directly, with no error to show for it.
    state_dir = _expanded(routing_cfg.get("collector_state_dir"))
    ttl = float(routing_cfg.get("quota_ttl_seconds", 300))
    out: dict[str, q.Quota] = {}
    for name, spec in providers.items():
        if reuse and state_dir:
            cached = q.from_collector(name, Path(str(state_dir)), ttl)
            if cached is not None:
                out[name] = cached
                continue
        out[name] = q.fetch_quota(name, spec, q.load_key(spec, env), timeout)
    return out


def ping(spec: dict, key: str, model_id: str, timeout: float = 40.0,
         max_tokens: int = 64) -> tuple[bool, float, str]:
    """One minimal request, to confirm a provider is actually answering.

    ``max_tokens`` must be generous enough for a reasoning model to emit
    something after thinking. At very small budgets (1-5) at least one provider
    here answers with HTTP 500 "empty response content" where others silently
    return a truncated success, so a tiny probe reports a healthy provider as
    dead. 64 is comfortably above the measured threshold of 20.
    """
    base = str(spec.get("base_url") or "").rstrip("/")
    body = json.dumps({"model": model_id, "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": max_tokens}).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "User-Agent": "ds-router/0.1"}
    header_name = spec.get("session_header")
    if header_name:
        headers[str(header_name)] = "ds-router-health"
    request = urllib.request.Request(f"{base}/chat/completions", data=body, headers=headers)
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(64)
            return True, time.time() - started, ""
    except urllib.error.HTTPError as exc:
        elapsed = time.time() - started
        # A provider that answered at all is reachable -- the point of the probe.
        # A 4xx/5xx carries a status, so Hermes' own error classification and
        # fallback handle it; treating it as "dead" here would wrongly exclude a
        # provider that is merely rejecting this one request shape.
        if exc.code in (400, 404, 422):
            return True, elapsed, f"reachable (HTTP {exc.code} on probe)"
        return False, elapsed, f"HTTP {exc.code}"
    except Exception as exc:
        return False, time.time() - started, f"{type(exc).__name__}: {exc}"[:120]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print the decision, proxy nothing")
    parser.add_argument("--model", default=None, help="model alias to route (default: config default)")
    parser.add_argument("--health", action="store_true", help="also time a real request per provider")
    parser.add_argument("--sticky", default=None,
                        help="provider to stay on unless it is exhausted (the current one)")
    parser.add_argument("--list-models", action="store_true", help="fetch each provider's model list")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    config = load_config()
    env = {**q.read_env(HERMES_ENV), **os.environ}
    providers = build_view(config, env)
    alias = args.model or config.get("default_model")
    routing_cfg = config.get("routing") or {}
    weights = routing_cfg.get("window_weights") or {"session": 1.0, "weekly": 1.0, "monthly": 0.6}
    skip_at = float(routing_cfg.get("skip_at", 0.85))

    if args.list_models:
        for name, spec in providers.items():
            key = q.load_key(spec, env)
            try:
                payload = q._http_json(f"{str(spec['base_url']).rstrip('/')}/models", key)
                ids = [m.get("id") for m in (payload.get("data") or payload.get("models") or [])]
                print(f"{name}: {len(ids)} models")
                for mid in ids:
                    print(f"    {mid}")
            except Exception as exc:
                print(f"{name}: could not list models ({type(exc).__name__}: {exc})")
        return 0

    quotas = collect(providers, env, routing_cfg=routing_cfg)
    peak_cfg = config.get("peak") or {}
    on_peak = p.peak_providers(peak_cfg) if peak_cfg.get("preferred_tiebreak") else set()

    conc_cfg = routing_cfg.get("concurrency") or {}
    caps = (conc_cfg.get("caps") or {}) if conc_cfg.get("enabled", True) else {}
    live_load = load_mod.active_by_provider() if caps else load_mod.Load({}, "disabled")

    # Health is measured BEFORE the decision so it can inform it: a provider that
    # is not answering cannot be spent down, so it is excluded rather than ranked.
    health: dict[str, dict] = {}
    if args.health:
        for name, spec in providers.items():
            model_id = (spec.get("models") or {}).get(alias)
            if not model_id:
                continue
            ok, seconds, err = ping(spec, q.load_key(spec, env), model_id)
            health[name] = {"ok": ok, "seconds": round(seconds, 2), "error": err}

    decision = r.choose(alias, providers, quotas, weights, skip_at,
                        sticky_provider=args.sticky, peak_providers=on_peak,
                        load=live_load.counts, concurrency_caps=caps,
                        pressure_per_over=float(conc_cfg.get("pressure_per_over", 0.08)),
                        max_load_pressure=float(conc_cfg.get("max_pressure", 0.4)),
                        health=health or None)

    if args.json:
        print(json.dumps({
            "model_alias": alias,
            "chosen": decision.provider,
            "model_id": decision.model_id,
            "reason": decision.reason,
            "active_sessions": live_load.counts,
            "concurrency_caps": caps,
            "load": {"source": live_load.source, "readable": live_load.readable,
                     "error": live_load.error},
            "health": health,
            "candidates": [
                {"provider": c.provider, "risk": round(c.pressure, 4), "headroom": round(c.headroom, 4),
                 "quota_ok": c.quota_ok, "hard": c.hard, "detail": c.detail}
                for c in decision.ranked
            ],
        }, indent=2))
        return 0

    print(f"\n  model alias : {alias}")
    print(f"  decision    : {decision.provider or 'NONE'}"
          + (f"  ->  {decision.model_id}" if decision.model_id else ""))
    print(f"  reason      : {decision.reason}")
    print(f"  peak now    : {', '.join(sorted(on_peak)) or 'none'}")
    over = load_mod.over_capacity(live_load, caps)
    load_desc = ", ".join(f"{n}={live_load.count(n)}/{caps.get(n, '-')}" for n in providers)
    print(f"  active      : {load_desc}  (via {live_load.source})")
    if not live_load.readable:
        # An unreadable store and an idle one both count zero, so say which this
        # is: the concurrency pressure term is silently absent otherwise.
        print(f"  load unknown: {live_load.error or live_load.source}"
              "  (no provider is charged for work already running on it)")
    if over:
        print(f"  OVER CAP    : " + ", ".join(f"{n} by {o}" for n, o in over.items()))
    print()
    print(f"  {'provider':<15}{'risk':>8}{'headroom':>10}   detail")
    print("  " + "-" * 84)
    for c in decision.ranked:
        flag = "EXHAUSTED" if c.hard else ("" if c.quota_ok else "no reading")
        print(f"  {c.provider:<15}{c.pressure:>8.3f}{c.headroom:>10.2f}   {c.detail} {flag}")
    if health:
        print()
        for name, row in health.items():
            state = f"{row['seconds']}s" if row["ok"] else f"FAILED ({row['error']})"
            print(f"  health {name:<15} {state}")
    print()
    return 0 if decision.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
