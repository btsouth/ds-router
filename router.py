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
from provider_http import open_request, safe_error

CONFIG = HERE / "config.yaml"
from paths import env_file as _hermes_env_file  # noqa: E402

HERMES_ENV = _hermes_env_file()


class ConfigError(RuntimeError):
    """config.yaml is missing, unreadable, or not the shape it claims to be."""


def load_config() -> dict:
    """This repo's config.yaml, validated enough to fail with a message rather than
    a traceback.

    apply.py has always done this and router.py never did, which is backwards: the
    README points a new user at `./router.py --dry-run` as the first thing to try,
    and a typo in the file it reads turned that into a raw stack trace.
    """
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("pyyaml is not installed, so config.yaml cannot be read "
                          "(install it with: python3 -m pip install --user pyyaml)") from exc

    try:
        raw = CONFIG.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read {CONFIG}: {exc}") from exc
    try:
        cfg = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{CONFIG.name} is not valid YAML: {str(exc)[:200]}") from exc
    if not isinstance(cfg, dict):
        raise ConfigError(f"{CONFIG.name} must be a mapping, found {type(cfg).__name__}")
    from config_schema import validate
    try:
        return validate(cfg)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


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
        if not spec.get("quota"):
            # Disabling a reader must also disable snapshots left by that reader.
            out[name] = q.fetch_quota(name, spec, q.load_key(spec, env), timeout)
            continue
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
        with open_request(request, timeout=timeout) as response:
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
        return False, time.time() - started, safe_error(exc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print the decision, proxy nothing")
    parser.add_argument("--model", default=None, help="model alias to route (default: config default)")
    parser.add_argument("--health", action="store_true", help="also time a real request per provider")
    parser.add_argument("--verify-sticky", action="store_true",
                        help="probe the sticky provider, but only when its reading failed, so a "
                             "provider that is really gone can still be left")
    parser.add_argument("--sticky", default=None,
                        help="provider to stay on unless it is exhausted (the current one)")
    parser.add_argument("--list-models", action="store_true", help="fetch each provider's model list")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 1
    env = {**q.read_env(HERMES_ENV), **os.environ}
    providers = build_view(config, env)
    alias = args.model or config.get("default_model")
    routing_cfg = config.get("routing") or {}
    weights = routing_cfg.get("window_weights") or {"session": 1.0, "weekly": 1.0, "monthly": 0.6}
    skip_at = float(routing_cfg.get("skip_at", 0.85))

    if args.list_models:
        failures = 0
        for name, spec in providers.items():
            key = q.load_key(spec, env)
            try:
                payload = q._http_json(f"{str(spec['base_url']).rstrip('/')}/models", key)
                ids = [m.get("id") for m in (payload.get("data") or payload.get("models") or [])]
                print(f"{name}: {len(ids)} models")
                for mid in ids:
                    print(f"    {mid}")
            except Exception as exc:
                failures += 1
                print(f"{name}: could not list models ({safe_error(exc)})")
        return 1 if failures or not providers else 0

    quotas = collect(providers, env, routing_cfg=routing_cfg)
    peak_cfg = config.get("peak") or {}
    on_peak: set = set()
    if peak_cfg.get("preferred_tiebreak"):
        try:
            on_peak = p.peak_providers(peak_cfg)
        except ValueError as exc:
            # Peak pricing is only ever a tie-break, so a malformed window must not
            # stop the router. It must not be silent either: without this note a
            # broken span reads as "never at peak" and the tie-break quietly
            # prefers the wrong provider.
            print(f"  note: ignoring the peak pricing config ({exc}).", file=sys.stderr)

    conc_cfg = routing_cfg.get("concurrency") or {}
    declared_caps = (conc_cfg.get("caps") or {}) if conc_cfg.get("enabled", True) else {}
    caps, cap_problems = load_mod.normalize_caps(declared_caps)
    if cap_problems:
        print("refusing to route: unreadable concurrency cap(s) in config.yaml: "
              + "; ".join(cap_problems), file=sys.stderr)
        print("  a cap is a positive integer, and a cap that cannot be read is not "
              "'unlimited'. Fix it or remove the entry.", file=sys.stderr)
        return 2
    unknown_caps = [name for name in caps if name not in providers]
    if unknown_caps:
        # A typo'd provider name means the limit is never enforced anywhere, which is
        # the same silent hole as a cap that cannot be read.
        print(f"  note: cap(s) declared for provider(s) not in config.yaml: "
              f"{', '.join(sorted(unknown_caps))} - they can never apply.",
              file=sys.stderr)
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
    elif args.verify_sticky and args.sticky:
        # The one case where a probe changes a decision rather than describing it: a
        # sticky provider whose reading failed. The rule is "a failed reading is not
        # a reason to move", which is only safe while something can still evict a
        # provider that is genuinely gone. Hard exhaustion needs a reading, and a
        # reading is exactly what is missing, so without this the provider is held
        # indefinitely on a broken key. One request, only while its reading is bad.
        spec = providers.get(args.sticky)
        stale = quotas.get(args.sticky)
        if spec is not None and (stale is None or stale.stale):
            model_id = (spec.get("models") or {}).get(alias)
            if model_id:
                ok, seconds, err = ping(spec, q.load_key(spec, env), model_id)
                health[args.sticky] = {"ok": ok, "seconds": round(seconds, 2), "error": err}

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
            # An empty health map means "not probed", not "all healthy", so say so
            # rather than leaving a caller to infer it from an empty object.
            "health_measured": bool(args.health),
            "health": health,
            "candidates": [
                # `usage` is the fullest window's usage fraction: 1.0 is spent and
                # 0.0 is empty. It was called "headroom", which reads the other way
                # round and made sorting on it pick the worst provider. null when
                # there is no reading, so the 9.9 sentinel cannot be charted as 990%.
                {"provider": c.provider, "risk": round(c.pressure, 4),
                 "usage": round(c.headroom, 4) if c.quota_ok else None,
                 "quota_ok": c.quota_ok, "hard": c.hard, "detail": c.detail}
                for c in decision.ranked
            ],
        }, indent=2))
        # Same contract as the text output: a refusal is exit 1 ("nothing usable"),
        # not a clean run with an empty "chosen". The document still goes out, and
        # apply.py reads a refusal out of it rather than mistaking it for a crash.
        return 0 if decision.ok else 1

    print(f"\n  model alias : {alias}")
    print(f"  decision    : {decision.provider or 'NONE'}"
          + (f"  ->  {decision.model_id}" if decision.model_id else ""))
    print(f"  reason      : {decision.reason}")
    print(f"  peak now    : {', '.join(sorted(on_peak)) or 'none'}")
    over = load_mod.over_capacity(live_load, caps)
    load_desc = ", ".join(f"{n}={live_load.count(n)}/{caps.get(n, '-')}" for n in providers)
    print(f"  active      : {load_desc}  (via {live_load.source})")
    if live_load.error:
        # Printed whether or not the reading was usable: a partial count is smaller
        # than reality, and a silent undercount looks like a healthy fleet.
        print(f"  load note   : {live_load.error}")
    if not live_load.readable:
        # An unreadable store and an idle one both count zero, so say which this
        # is: the concurrency pressure term is silently absent otherwise.
        print(f"  load unknown: {live_load.error or live_load.source}"
              "  (no provider is charged for work already running on it)")
    if over:
        print(f"  OVER CAP    : " + ", ".join(f"{n} by {o}" for n, o in over.items()))
    print()
    # "usage", not "headroom": this is the fullest window's usage fraction, so 0 is
    # the best value and 1.0 is spent. The old name read the other way round.
    print(f"  {'provider':<15}{'risk':>8}{'usage':>10}   detail")
    print("  " + "-" * 84)
    for c in decision.ranked:
        flag = "no reading" if not c.quota_ok else ("EXHAUSTED" if c.hard else "")
        # The 9.9 sentinel exists so it cannot be charted; printing it in a column
        # headed "usage" hands the reader a number where the JSON says null.
        usage = f"{c.headroom:.2f}" if c.quota_ok else "-"
        print(f"  {c.provider:<15}{c.pressure:>8.3f}{usage:>10}   {c.detail} {flag}")
    if health:
        print()
        for name, row in health.items():
            state = f"{row['seconds']}s" if row["ok"] else f"FAILED ({row['error']})"
            print(f"  health {name:<15} {state}")
    print()
    return 0 if decision.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
