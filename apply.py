#!/usr/bin/env python3
"""Apply the router's decision to Hermes.

Reads everything from this repo's config.yaml, so provider endpoints and model
ids have exactly one definition. The shell wrapper (ds-switch) only parses
arguments and delegates here.

    apply.py                 apply the router's recommendation
    apply.py --show          print the decision without writing
    apply.py --off           hand routing back to the default provider
    apply.py <provider>      pin a provider explicitly
    apply.py --check         report whether the Hermes config looks consistent
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CONFIG = HERE / "config.yaml"


def load_config() -> dict:
    import yaml
    return yaml.safe_load(CONFIG.read_text())


def hermes(*args: str) -> str:
    """Run a hermes CLI command, returning stripped stdout."""
    proc = subprocess.run(["hermes", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"hermes {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def current_provider() -> str:
    """The provider Hermes is configured to use, or '' when unset."""
    try:
        out = hermes("config", "get", "model.provider")
    except Exception:
        return ""
    # `hermes config get` prints the value, or a "not set" notice.
    for line in reversed(out.splitlines()):
        value = line.strip()
        if value and not value.lower().startswith("config key not set"):
            return value
    return ""


def router_decision(sticky: str | None, alias: str) -> dict:
    """Ask router.py for a decision. Raises on failure."""
    args = ["./router.py", "--dry-run", "--json", "--model", alias]
    if sticky:
        args += ["--sticky", sticky]
    proc = subprocess.run(args, capture_output=True, text=True, cwd=str(HERE))
    if proc.returncode != 0:
        raise RuntimeError(f"router exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def model_id_for(provider: str, alias: str, models: dict) -> str:
    """The id *provider* calls *alias*, or '' when it does not serve it.

    The config keeps one alias->provider->id table, so a provider serves an
    alias when models[alias][provider] is set.
    """
    table = models.get(alias) or {}
    return str(table.get(provider) or "")


def set_provider(name: str, spec: dict, alias: str, *, dry: bool,
                 models: dict | None = None) -> tuple[str, str]:
    """Write model.provider/default/base_url for one provider.

    Returns (model_id, base_url). Raises when the alias is not served there,
    rather than writing a provider with no usable model.
    """
    model_id = model_id_for(name, alias, models or {})
    if not model_id:
        offered = ", ".join(sorted(k for k, t in (models or {}).items() if (t or {}).get(name))) or "none"
        raise RuntimeError(
            f"provider {name!r} does not serve alias {alias!r} (it serves: {offered})"
        )
    base_url = str(spec.get("base_url") or "")
    if not dry:
        hermes("config", "set", "model.provider", name)
        hermes("config", "set", "model.default", model_id)
        if base_url:
            hermes("config", "set", "model.base_url", base_url)
    return model_id, base_url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("provider", nargs="?", help="pin a provider explicitly")
    ap.add_argument("--show", action="store_true", help="print the decision, write nothing")
    ap.add_argument("--off", action="store_true", help="hand routing back to the default provider")
    ap.add_argument("--check", action="store_true", help="report config consistency, write nothing")
    ap.add_argument("--alias", default=None, help="model alias to route")
    args = ap.parse_args()

    cfg = load_config()
    providers = cfg.get("providers") or {}
    models = cfg.get("models") or {}
    alias = args.alias or cfg.get("default_model")
    default_provider = str(cfg.get("default_provider") or "")

    if not providers:
        print("No providers declared in config.yaml", file=sys.stderr)
        return 1

    if args.check:
        return check(cfg, providers, models, alias)

    if args.off:
        if not default_provider or default_provider not in providers:
            print("config.yaml needs a 'default_provider' naming one of: "
                  + ", ".join(providers), file=sys.stderr)
            return 2
        model_id, _ = set_provider(default_provider, providers[default_provider], alias,
                                   dry=args.show, models=models)
        print(f"Routing off — Hermes is back on {default_provider} ({model_id})."
              + ("  [--show: nothing written]" if args.show else ""))
        return 0

    if args.provider:
        chosen, reason = args.provider, "pinned by hand"
        if chosen not in providers:
            print(f"Unknown provider: {chosen}", file=sys.stderr)
            print("Known: " + ", ".join(providers), file=sys.stderr)
            return 2
    else:
        # Stay on whatever Hermes already uses unless there is a real reason to
        # move: each move rebuilds the agent and resets the prompt cache.
        sticky = current_provider()
        if sticky not in providers:
            sticky = None
        try:
            decision = router_decision(sticky, alias)
        except Exception as exc:
            print(f"Router failed to produce a decision ({exc}); leaving Hermes config untouched.",
                  file=sys.stderr)
            return 3
        chosen = decision.get("chosen") or ""
        reason = decision.get("reason") or ""
        if not chosen:
            print(f"Router refused to choose: {reason}", file=sys.stderr)
            print("Leaving Hermes config untouched.", file=sys.stderr)
            return 4

    try:
        model_id, _ = set_provider(chosen, providers[chosen], alias, dry=args.show, models=models)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 5

    if args.show:
        print(f"Would set: provider={chosen} model={model_id}")
        print(f"Reason:    {reason}")
        return 0

    print(f"Routed to {chosen} ({model_id})")
    print(f"  reason: {reason}")
    return 0


def check(cfg: dict, providers: dict, models: dict, alias: str) -> int:
    """Report whether the config can actually drive Hermes. Writes nothing."""
    problems, notes = [], []
    for name, spec in providers.items():
        if not spec.get("base_url"):
            problems.append(f"{name}: no base_url")
        if not spec.get("key_env"):
            problems.append(f"{name}: no key_env")
        elif not _env_present(str(spec["key_env"])):
            notes.append(f"{name}: key_env {spec['key_env']} is not set in this shell "
                         f"(fine if Hermes loads it from .env)")
        model_id = model_id_for(name, alias, models)
        if not model_id:
            notes.append(f"{name}: does not serve alias {alias!r}")
        else:
            notes.append(f"{name}: {alias} -> {model_id}")

    default_provider = str(cfg.get("default_provider") or "")
    if default_provider not in providers:
        problems.append(f"default_provider {default_provider!r} is not one of: {', '.join(providers)}")

    seen = str(cfg.get("default_model") or "")
    if seen != alias:
        problems.append(f"default_model {seen!r} != alias {alias!r} (router runs with {alias!r})")

    print(f"alias            : {alias}")
    print(f"default_provider : {default_provider or '(unset)'}")
    print(f"providers        : {len(providers)} ({', '.join(providers)})")
    for note in notes:
        print(f"  note  {note}")
    for problem in problems:
        print(f"  ERROR {problem}")
    print("config OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


def _env_present(name: str) -> bool:
    import os
    if os.environ.get(name):
        return True
    env_file = Path.home() / ".hermes" / ".env"
    try:
        return any(line.split("=", 1)[0].strip() == name
                   for line in env_file.read_text().splitlines()
                   if "=" in line and not line.strip().startswith("#"))
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
